from __future__ import annotations

import uuid
from datetime import timedelta
from pathlib import Path

from scripts.core.audit import AuditLog, append_jsonl
from scripts.core.models import Order, Quote, parse_ts, utc_now
from scripts.core.state import JsonStateStore
from scripts.journal.trade_lifecycle import TradeLifecycleJournal
from scripts.risk.risk_gate import check_order
from scripts.simulation.fill_model import simulate_fill
from scripts.simulation.virtual_account import apply_fill


class PaperBroker:
    def __init__(self, root: str | Path, config: dict, *, namespace: str | None = None) -> None:
        self.root = Path(root)
        self.config = config
        self.namespace = namespace
        self.store = JsonStateStore(
            self.root,
            float(config["paper"].get("paper_initial_cash_usd", 2000)),
            namespace=namespace,
        )
        self.log_prefix = f"strategy_sleeves/{namespace}/" if namespace else ""
        self.audit = AuditLog(self.root, f"{self.log_prefix}audit.jsonl")
        self.lifecycle = TradeLifecycleJournal(self.root, namespace=namespace)
        self.store.ensure()

    def create_order(
        self,
        *,
        decision_id: str,
        symbol: str,
        side: str,
        order_type: str,
        quantity: float,
        limit_price: float | None,
        quote_seen_at: str,
        thesis: str = "",
        idempotency_key: str | None = None,
        now: str | None = None,
    ) -> Order:
        order = Order(
            order_id=f"po_{uuid.uuid4().hex}",
            decision_id=decision_id,
            symbol=symbol.upper(),
            side=side,  # type: ignore[arg-type]
            order_type=order_type,  # type: ignore[arg-type]
            quantity=float(quantity),
            limit_price=float(limit_price) if limit_price is not None else None,
            quote_seen_at=quote_seen_at,
            idempotency_key=idempotency_key or decision_id,
            thesis=thesis,
            created_at=now or utc_now(),
        )
        orders = self.store.orders()
        orders[order.order_id] = order
        self.store.save_orders(orders)
        self.audit.append("paper_order_created", {"order": order.to_dict()})
        return order

    def submit_order(self, order: Order, quote: Quote | None, now: str | None = None) -> Order:
        now = now or utc_now()
        orders = self.store.orders()
        account = self.store.account()
        positions = self.store.positions()
        counters = self.store.daily_counters(now)
        open_orders = {oid: current for oid, current in orders.items() if oid != order.order_id}

        order.status = "submitted_to_paper_broker"
        order.submitted_at = now
        order.updated_at = now
        orders[order.order_id] = order
        self.store.save_orders(orders)

        option_positions = self.store.read_json("paper_option_positions.json", {})
        option_orders = self.store.read_json("paper_option_orders.json", {})
        risk = check_order(
            order,
            quote,
            account,
            positions,
            open_orders,
            counters,
            self.config,
            now,
            option_positions=option_positions,
            option_orders=option_orders,
        )
        if not risk.approved:
            order.status = "rejected"
            order.reject_reason = risk.reason
            order.updated_at = now
            orders[order.order_id] = order
            self.store.save_orders(orders)
            self.audit.append("paper_order_rejected", {"reason": risk.reason, "order": order.to_dict()})
            return order

        assert quote is not None
        fill_decision = simulate_fill(order, quote, self.config["costs"], filled_at=now)
        if fill_decision.status == "open":
            order.status = "open"
            order.updated_at = now
            orders[order.order_id] = order
            self.store.save_orders(orders)
            append_jsonl(self.root, f"{self.log_prefix}paper_orders.jsonl", {"event": "open", "order": order.to_dict(), "quote": quote.to_dict()})
            self.audit.append("paper_order_open", {"reason": fill_decision.reason, "order": order.to_dict()})
            return order
        if fill_decision.status == "rejected":
            order.status = "rejected"
            order.reject_reason = fill_decision.reason
            order.updated_at = now
            orders[order.order_id] = order
            self.store.save_orders(orders)
            self.audit.append("paper_order_rejected", {"reason": fill_decision.reason, "order": order.to_dict()})
            return order

        fill = fill_decision.fill
        assert fill is not None
        realized_before = account.realized_pnl
        try:
            apply_fill(account, positions, fill)
        except ValueError as exc:
            order.status = "rejected"
            order.reject_reason = str(exc)
            order.updated_at = now
            orders[order.order_id] = order
            self.store.save_orders(orders)
            self.audit.append("paper_order_rejected", {"reason": str(exc), "order": order.to_dict()})
            return order

        order.status = "filled"
        order.filled_quantity = fill.quantity
        order.average_fill_price = fill.price
        order.updated_at = fill.filled_at
        orders[order.order_id] = order
        self.store.save_account(account, fill.filled_at)
        self.store.save_positions(positions)
        self.store.save_orders(orders)
        if fill.side == "buy":
            self.store.increment_trades(now, line="equity")
        else:
            self.store.add_daily_realized_pnl(account.realized_pnl - realized_before, now, line="equity")
        append_jsonl(self.root, f"{self.log_prefix}paper_orders.jsonl", {"event": "filled", "order": order.to_dict(), "quote": quote.to_dict()})
        append_jsonl(self.root, f"{self.log_prefix}paper_fills.jsonl", {"fill": fill.to_dict(), "quote": quote.to_dict()})
        self.lifecycle.record_equity_fill(fill, order.thesis)
        self.audit.append("paper_order_filled", {"order": order.to_dict(), "fill": fill.to_dict(), "quote": quote.to_dict()})
        return order

    def process_open_orders(self, quotes: dict[str, Quote], now: str | None = None) -> list[Order]:
        now = now or utc_now()
        expiry_seconds = int(self.config["paper"].get("open_order_expiry_seconds", 300))
        processed: list[Order] = []
        for order in list(self.store.orders().values()):
            if order.status not in ("open", "submitted_to_paper_broker", "partially_filled"):
                continue
            submitted = parse_ts(order.submitted_at or order.created_at)
            if parse_ts(now) - submitted >= timedelta(seconds=expiry_seconds):
                orders = self.store.orders()
                current = orders[order.order_id]
                current.status = "expired"
                current.reject_reason = "paper order expired before fill"
                current.updated_at = now
                orders[current.order_id] = current
                self.store.save_orders(orders)
                self.audit.append("paper_order_expired", {"order": current.to_dict()})
                processed.append(current)
                continue
            processed.append(self.submit_order(order, quotes.get(order.symbol), now=now))
        return processed

    def cancel_order(self, order_id: str, reason: str = "cancelled") -> Order:
        orders = self.store.orders()
        order = orders[order_id]
        if order.status in ("filled", "cancelled", "rejected", "expired"):
            return order
        order.status = "cancelled"
        order.reject_reason = reason
        order.updated_at = utc_now()
        orders[order_id] = order
        self.store.save_orders(orders)
        self.audit.append("paper_order_cancelled", {"reason": reason, "order": order.to_dict()})
        return order
