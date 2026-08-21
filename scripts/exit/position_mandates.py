from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from scripts.core.audit import append_jsonl
from scripts.core.models import parse_ts, utc_now
from scripts.core.state import JsonStateStore
from scripts.runtime.market_clock import UsEquityMarketClock


_TERMINAL_ORDER_STATUSES = {"cancelled", "expired", "rejected"}


@dataclass(frozen=True)
class MandateExitDecision:
    should_exit: bool
    reason: str


def evaluate_mandate_exit(
    mandate: dict[str, Any] | None,
    now: str,
) -> MandateExitDecision:
    if not isinstance(mandate, dict):
        return MandateExitDecision(True, "missing position mandate; fail closed")
    required = {
        "exposure_id",
        "ticker",
        "instrument_type",
        "horizon",
        "planned_exit_at",
        "thesis_valid_until",
    }
    if required - set(mandate) or mandate.get("status") != "open":
        return MandateExitDecision(True, "invalid position mandate; fail closed")
    if mandate.get("invalidation_triggered", False):
        return MandateExitDecision(True, "thesis invalidation")
    current = parse_ts(now)
    try:
        planned_exit = parse_ts(str(mandate["planned_exit_at"]))
        thesis_valid_until = parse_ts(str(mandate["thesis_valid_until"]))
    except (TypeError, ValueError):
        return MandateExitDecision(True, "invalid position mandate; fail closed")
    if current >= planned_exit:
        return MandateExitDecision(True, "position mandate planned exit reached")
    if current >= thesis_valid_until:
        return MandateExitDecision(True, "position mandate thesis validity expired")
    return MandateExitDecision(False, "position mandate remains valid")


def planned_exit_time(
    entered_at: str,
    horizon: str,
    *,
    max_holding_trading_days: int,
    minutes_before_close: int,
) -> str:
    clock = UsEquityMarketClock()
    entered = parse_ts(entered_at)
    session = clock.calendar.date_to_session(pd.Timestamp(entered.date()), direction="next")
    if horizon == "intraday_close":
        target = session
    elif horizon == "next_close":
        target = clock.calendar.next_session(session)
    elif horizon == "two_to_five_days":
        target = session
        for _ in range(max(2, min(5, int(max_holding_trading_days)))):
            target = clock.calendar.next_session(target)
    else:
        raise ValueError("unsupported mandate horizon")
    close = clock.calendar.session_close(target).to_pydatetime()
    return (close - timedelta(minutes=minutes_before_close)).isoformat()


class PositionMandateStore:
    def __init__(
        self,
        root: str | Path,
        *,
        namespace: str = "ai_instrument_allocator_v1",
    ) -> None:
        self.root = Path(root)
        self.namespace = namespace
        self.store = JsonStateStore(self.root, namespace=namespace)
        self.log_name = f"strategy_sleeves/{namespace}/position_mandates.jsonl"

    def mandates(self) -> dict[str, dict[str, Any]]:
        raw = self.store.read_json("position_mandates.json", {})
        if not isinstance(raw, dict):
            return {}
        return {
            str(key): dict(value)
            for key, value in raw.items()
            if isinstance(value, dict)
        }

    def register_order(
        self,
        *,
        order_id: str,
        exposure_id: str,
        strategy: str,
        snapshot_id: str,
        ticker: str,
        instrument_type: str,
        horizon: str,
        created_at: str,
        planned_exit_at: str,
        thesis_valid_until: str,
        invalidation_condition: str,
        planned_stop_price: float | None,
    ) -> dict[str, Any]:
        if strategy != self.namespace:
            raise ValueError("position mandate namespace mismatch")
        if horizon not in {"intraday_close", "next_close", "two_to_five_days"}:
            raise ValueError("unsupported mandate horizon")
        parse_ts(created_at)
        parse_ts(planned_exit_at)
        parse_ts(thesis_valid_until)
        values = self.mandates()
        mandate = {
            "mandate_version": 1,
            "exposure_id": exposure_id,
            "order_id": order_id,
            "strategy": strategy,
            "snapshot_id": snapshot_id,
            "ticker": ticker.upper(),
            "instrument_type": instrument_type,
            "horizon": horizon,
            "status": "pending_fill",
            "created_at": created_at,
            "entered_at": None,
            "planned_exit_at": planned_exit_at,
            "thesis_valid_until": thesis_valid_until,
            "invalidation_condition": invalidation_condition,
            "invalidation_triggered": False,
            "planned_stop_price": planned_stop_price,
            "closed_at": None,
            "close_reason": None,
        }
        values[exposure_id] = mandate
        self.store.write_json("position_mandates.json", values)
        self._append("position_mandate_registered", mandate)
        return mandate

    def reconcile(
        self,
        *,
        equity_orders: dict[str, Any],
        option_orders: dict[str, Any],
        now: str,
    ) -> list[dict[str, Any]]:
        values = self.mandates()
        changed: list[dict[str, Any]] = []
        all_orders = {**equity_orders, **option_orders}
        for exposure_id, mandate in values.items():
            if mandate.get("status") != "pending_fill":
                continue
            order = all_orders.get(str(mandate.get("order_id")))
            if order is None:
                continue
            status = _value(order, "status")
            if status == "filled":
                mandate["status"] = "open"
                mandate["entered_at"] = str(
                    _value(order, "updated_at")
                    or _value(order, "submitted_at")
                    or now
                )
            elif status in _TERMINAL_ORDER_STATUSES:
                mandate["status"] = "closed"
                mandate["closed_at"] = now
                mandate["close_reason"] = f"entry order {status}"
            else:
                continue
            values[exposure_id] = mandate
            changed.append(dict(mandate))
            self._append("position_mandate_reconciled", mandate)
        if changed:
            self.store.write_json("position_mandates.json", values)
        return changed

    def for_exposure(self, exposure_id: str) -> dict[str, Any] | None:
        return self.mandates().get(exposure_id)

    def mark_invalidation(
        self,
        exposure_id: str,
        *,
        reason: str,
        now: str | None = None,
    ) -> dict[str, Any] | None:
        values = self.mandates()
        mandate = values.get(exposure_id)
        if mandate is None:
            return None
        mandate["invalidation_triggered"] = True
        mandate["invalidation_reason"] = reason
        mandate["updated_at"] = now or utc_now()
        values[exposure_id] = mandate
        self.store.write_json("position_mandates.json", values)
        self._append("position_mandate_invalidated", mandate)
        return mandate

    def close(
        self,
        exposure_id: str,
        *,
        reason: str,
        now: str,
    ) -> dict[str, Any] | None:
        values = self.mandates()
        mandate = values.get(exposure_id)
        if mandate is None:
            return None
        mandate["status"] = "closed"
        mandate["closed_at"] = now
        mandate["close_reason"] = reason
        values[exposure_id] = mandate
        self.store.write_json("position_mandates.json", values)
        self._append("position_mandate_closed", mandate)
        return mandate

    def _append(self, event: str, mandate: dict[str, Any]) -> None:
        append_jsonl(
            self.root,
            self.log_name,
            {"event": event, "namespace": self.namespace, "mandate": mandate},
        )


def _value(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)
