from __future__ import annotations

import uuid
from datetime import timedelta
from pathlib import Path

from scripts.core.audit import AuditLog, append_jsonl
from scripts.core.models import parse_ts, utc_now
from scripts.journal.trade_lifecycle import TradeLifecycleJournal
from scripts.options.fill_model import simulate_option_fill
from scripts.options.models import OptionContract, OptionOrder, OptionQuote
from scripts.options.risk_gate import check_option_order
from scripts.options.state import OptionStateStore
from scripts.options.virtual_account import apply_option_fill
from scripts.simulation.fill_transaction import (
    PaperFillTransactionCoordinator,
    counters_after_fill,
)


class OptionPaperBroker:
    """Local long-premium broker. It has no live broker order methods."""

    def __init__(
        self,
        root: str | Path,
        config: dict,
        *,
        namespace: str | None = None,
        initial_cash: float | None = None,
    ) -> None:
        self.root = Path(root)
        self.config = config
        self.namespace = namespace
        self.store = OptionStateStore(
            self.root,
            float(
                initial_cash
                if initial_cash is not None
                else config["paper"].get("paper_initial_cash_usd", 2000)
            ),
            namespace=namespace,
        )
        self.log_prefix = f"strategy_sleeves/{namespace}/" if namespace else ""
        self.audit = AuditLog(self.root, f"{self.log_prefix}audit.jsonl")
        self.lifecycle = TradeLifecycleJournal(self.root, namespace=namespace)
        self.transactions = PaperFillTransactionCoordinator(self.store.base)
        self.transactions.recover()

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
        strategy: str | None = None,
        signal_horizon: str | None = None,
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
            strategy=strategy,
            signal_horizon=signal_horizon,
            created_at=now or utc_now(),
        )
        with self.transactions.lock():
            self.transactions.recover_locked()
            orders = self.store.orders()
            orders[order.order_id] = order
            self.store.save_orders(orders)
        self.audit.append("paper_option_order_created", {"order": order.to_dict()})
        return order

    def submit_order(
        self,
        order: OptionOrder,
        quote: OptionQuote | None,
        now: str | None = None,
        *,
        entry_nav_usd: float | None = None,
    ) -> OptionOrder:
        with self.transactions.lock():
            self.transactions.recover_locked()
            return self._submit_order_locked(
                order,
                quote,
                now,
                entry_nav_usd=entry_nav_usd,
            )

    def _submit_order_locked(
        self,
        order: OptionOrder,
        quote: OptionQuote | None,
        now: str | None = None,
        *,
        entry_nav_usd: float | None = None,
    ) -> OptionOrder:
        now = now or utc_now()
        orders = self.store.orders()
        persisted = orders.get(order.order_id)
        if persisted is not None and persisted.status in {
            "filled",
            "cancelled",
            "rejected",
            "expired",
        }:
            return persisted
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
            entry_nav_usd=entry_nav_usd,
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
            append_jsonl(self.root, f"{self.log_prefix}paper_option_orders.jsonl", {"event": "open", "order": order.to_dict(), "quote": quote.to_dict()})
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
        account.updated_at = fill.filled_at
        updated_counters = counters_after_fill(
            counters,
            line="options",
            is_entry=fill.intent == "buy_to_open",
            realized_pnl_delta=account.realized_pnl - realized_before,
        )
        lifecycle = self.lifecycle.prepare_option_fill(fill, order.thesis)
        lifecycle_writes = self.lifecycle.transaction_writes(
            lifecycle,
            transaction_id=fill.fill_id,
            ts=fill.filled_at,
        )
        self.transactions.commit_locked(
            {
                "transaction_id": fill.fill_id,
                "order_id": order.order_id,
                "instrument": f"option_{fill.option_type}",
                "prepared_at": fill.filled_at,
                "fill": fill.to_dict(),
                "state_writes": [
                    {"name": "paper_account.json", "data": account.to_dict()},
                    {
                        "name": "paper_option_positions.json",
                        "data": {
                            option_id: position.to_dict()
                            for option_id, position in option_positions.items()
                        },
                    },
                    {
                        "name": "paper_option_orders.json",
                        "data": {
                            order_id: current.to_dict()
                            for order_id, current in orders.items()
                        },
                    },
                    {"name": "daily_counters.json", "data": updated_counters},
                    lifecycle_writes["state_write"],
                ],
                "jsonl_writes": [
                    {
                        "filename": f"{self.log_prefix}paper_option_orders.jsonl",
                        "record": {
                            "ts": fill.filled_at,
                            "event": "filled",
                            "order": order.to_dict(),
                            "quote": quote.to_dict(),
                        },
                    },
                    {
                        "filename": f"{self.log_prefix}paper_option_fills.jsonl",
                        "record": {
                            "ts": fill.filled_at,
                            "fill": fill.to_dict(),
                            "contract": order.contract.to_dict(),
                            "quote": quote.to_dict(),
                        },
                    },
                    lifecycle_writes["jsonl_write"],
                    {
                        "filename": f"{self.log_prefix}audit.jsonl",
                        "record": {
                            "audit_id": f"pa_fill_{fill.fill_id}",
                            "ts": fill.filled_at,
                            "event_type": "paper_option_order_filled",
                            "payload": {
                                "order": order.to_dict(),
                                "fill": fill.to_dict(),
                                "quote": quote.to_dict(),
                            },
                        },
                    },
                ],
                "text_writes": lifecycle_writes["text_writes"],
            }
        )
        return order

    def process_open_orders(
        self,
        quotes: dict[str, OptionQuote],
        now: str | None = None,
        *,
        entry_nav_usd: float | None = None,
    ) -> list[OptionOrder]:
        now = now or utc_now()
        expiry_seconds = int(self.config.get("options_costs", {}).get("open_order_expiry_seconds", 120))
        processed: list[OptionOrder] = []
        for order in list(self.store.orders().values()):
            if order.status not in {"open", "submitted_to_paper_broker", "partially_filled"}:
                continue
            submitted = parse_ts(order.submitted_at or order.created_at)
            if parse_ts(now) - submitted >= timedelta(seconds=expiry_seconds):
                with self.transactions.lock():
                    self.transactions.recover_locked()
                    orders = self.store.orders()
                    current = orders[order.order_id]
                    if current.status not in {
                        "open",
                        "submitted_to_paper_broker",
                        "partially_filled",
                    }:
                        processed.append(current)
                        continue
                    current.status = "expired"
                    current.reject_reason = "paper option order expired before fill"
                    current.updated_at = now
                    orders[current.order_id] = current
                    self.store.save_orders(orders)
                self.audit.append("paper_option_order_expired", {"order": current.to_dict()})
                processed.append(current)
            else:
                processed.append(
                    self.submit_order(
                        order,
                        quotes.get(order.contract.option_id),
                        now,
                        entry_nav_usd=entry_nav_usd,
                    )
                )
        return processed

    def cancel_order(
        self,
        order_id: str,
        reason: str = "cancelled",
        *,
        now: str | None = None,
    ) -> OptionOrder:
        with self.transactions.lock():
            self.transactions.recover_locked()
            orders = self.store.orders()
            order = orders[order_id]
            if order.status in {"filled", "cancelled", "rejected", "expired"}:
                return order
            order.status = "cancelled"
            order.reject_reason = reason
            order.updated_at = now or utc_now()
            orders[order_id] = order
            self.store.save_orders(orders)
        self.audit.append("paper_option_order_cancelled", {"reason": reason, "order": order.to_dict()})
        return order
