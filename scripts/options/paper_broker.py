from __future__ import annotations

import uuid
from datetime import timedelta
from pathlib import Path

from scripts.core.audit import AuditLog, append_jsonl
from scripts.core.models import parse_ts, utc_now
from scripts.options.fill_model import simulate_option_fill
from scripts.options.models import OptionContract, OptionOrder, OptionQuote
from scripts.options.risk_gate import check_option_order
from scripts.options.state import OptionStateStore
from scripts.options.virtual_account import apply_option_fill


class OptionPaperBroker:
    """Local long-premium broker. It has no live broker order methods."""

    def __init__(self, root: str | Path, config: dict) -> None:
        self.root = Path(root)
        self.config = config
        self.store = OptionStateStore(self.root, float(config["paper"].get("paper_initial_cash_usd", 2000)))
        self.audit = AuditLog(self.root)

    def create_order(
        self,
        *,
        decision_id: str,
        contract: OptionContract,
        intent: str,
        order_type: str,
        quantity: int,
        limit_price: float | None,
        quote_seen_at: str,
        thesis: str = "",
        idempotency_key: str | None = None,
        now: str | None = None,
    ) -> OptionOrder:
        order = OptionOrder(
            order_id=f"opo_{uuid.uuid4().hex}",
            decision_id=decision_id,
            contract=contract,
            intent=intent,  # type: ignore[arg-type]
            order_type=order_type,  # type: ignore[arg-type]
            quantity=int(quantity),
            limit_price=float(limit_price) if limit_price is not None else None,
            quote_seen_at=quote_seen_at,
            idempotency_key=idempotency_key or decision_id,
            thesis=thesis,
            created_at=now or utc_now(),
        )
        orders = self.store.orders()
        orders[order.order_id] = order
        self.store.save_orders(orders)
        self.audit.append("paper_option_order_created", {"order": order.to_dict()})
        return order

    def submit_order(self, order: OptionOrder, quote: OptionQuote | None, now: str | None = None) -> OptionOrder:
        now = now or utc_now()
        orders = self.store.orders()
        account = self.store.base.account()
        equity_positions = self.store.base.positions()
        option_positions = self.store.positions()
        equity_orders = self.store.base.orders()
        counters = self.store.base.daily_counters(now)
        other_orders = {key: value for key, value in orders.items() if key != order.order_id}

        order.status = "submitted_to_paper_broker"
        order.submitted_at = order.submitted_at or now
        order.updated_at = now
        orders[order.order_id] = order
        self.store.save_orders(orders)

        risk = check_option_order(
            order,
            quote,
            account,
            equity_positions,
            option_positions,
            equity_orders,
            other_orders,
            counters,
            self.config,
            now,
        )
        if not risk.approved:
            order.status = "rejected"
            order.reject_reason = risk.reason
            orders[order.order_id] = order
            self.store.save_orders(orders)
            self.audit.append("paper_option_order_rejected", {"reason": risk.reason, "order": order.to_dict()})
            return order

        assert quote is not None
        decision = simulate_option_fill(order, quote, self.config.get("options_costs", {}), now)
        if decision.status == "open":
            order.status = "open"
            order.updated_at = now
            orders[order.order_id] = order
            self.store.save_orders(orders)
            append_jsonl(self.root, "paper_option_orders.jsonl", {"event": "open", "order": order.to_dict(), "quote": quote.to_dict()})
            return order
        if decision.status == "rejected" or decision.fill is None:
            order.status = "rejected"
            order.reject_reason = decision.reason or "option fill rejected"
            orders[order.order_id] = order
            self.store.save_orders(orders)
            return order

        fill = decision.fill
        realized_before = account.realized_pnl
        try:
            apply_option_fill(account, option_positions, fill, order.contract)
        except ValueError as exc:
            order.status = "rejected"
            order.reject_reason = str(exc)
            orders[order.order_id] = order
            self.store.save_orders(orders)
            self.audit.append("paper_option_order_rejected", {"reason": str(exc), "order": order.to_dict()})
            return order

        order.status = "filled"
        order.filled_quantity = fill.quantity
        order.average_fill_price = fill.price
        order.updated_at = fill.filled_at
        orders[order.order_id] = order
        self.store.base.save_account(account, fill.filled_at)
        self.store.save_positions(option_positions)
        self.store.save_orders(orders)
        self.store.base.increment_trades(now, line="options")
        if fill.intent == "sell_to_close":
            self.store.base.add_daily_realized_pnl(account.realized_pnl - realized_before, now, line="options")
        append_jsonl(self.root, "paper_option_orders.jsonl", {"event": "filled", "order": order.to_dict(), "quote": quote.to_dict()})
        append_jsonl(self.root, "paper_option_fills.jsonl", {"fill": fill.to_dict(), "contract": order.contract.to_dict(), "quote": quote.to_dict()})
        self.audit.append("paper_option_order_filled", {"order": order.to_dict(), "fill": fill.to_dict(), "quote": quote.to_dict()})
        return order

    def process_open_orders(self, quotes: dict[str, OptionQuote], now: str | None = None) -> list[OptionOrder]:
        now = now or utc_now()
        expiry_seconds = int(self.config.get("options_costs", {}).get("open_order_expiry_seconds", 120))
        processed: list[OptionOrder] = []
        for order in list(self.store.orders().values()):
            if order.status not in {"open", "submitted_to_paper_broker", "partially_filled"}:
                continue
            submitted = parse_ts(order.submitted_at or order.created_at)
            if parse_ts(now) - submitted >= timedelta(seconds=expiry_seconds):
                orders = self.store.orders()
                current = orders[order.order_id]
                current.status = "expired"
                current.reject_reason = "paper option order expired before fill"
                current.updated_at = now
                orders[current.order_id] = current
                self.store.save_orders(orders)
                self.audit.append("paper_option_order_expired", {"order": current.to_dict()})
                processed.append(current)
            else:
                processed.append(self.submit_order(order, quotes.get(order.contract.option_id), now))
        return processed

    def cancel_order(self, order_id: str, reason: str = "cancelled") -> OptionOrder:
        orders = self.store.orders()
        order = orders[order_id]
        if order.status in {"filled", "cancelled", "rejected", "expired"}:
            return order
        order.status = "cancelled"
        order.reject_reason = reason
        order.updated_at = utc_now()
        orders[order_id] = order
        self.store.save_orders(orders)
        self.audit.append("paper_option_order_cancelled", {"reason": reason, "order": order.to_dict()})
        return order
