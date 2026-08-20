from __future__ import annotations

from pathlib import Path
from typing import Any

from scripts.core.audit import append_jsonl
from scripts.core.models import parse_ts, utc_now
from scripts.core.state import JsonStateStore


def _record_mapping(raw: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, dict):
        return {}
    return {
        str(key): dict(value)
        for key, value in raw.items()
        if isinstance(value, dict)
    }


class AllocatorStateStore:
    """Restart-safe conditional plans and allocations for one isolated sleeve."""

    def __init__(
        self,
        root: str | Path,
        *,
        namespace: str = "ai_instrument_allocator_v1",
    ) -> None:
        self.root = Path(root)
        self.namespace = namespace
        self.store = JsonStateStore(self.root, namespace=namespace)
        self.log_name = f"strategy_sleeves/{namespace}/allocator_state.jsonl"

    def save_plan(self, plan: dict[str, Any]) -> dict[str, Any]:
        required = {
            "plan_id",
            "strategy",
            "ticker",
            "created_at",
            "valid_until",
            "status",
            "signal",
            "snapshot",
        }
        missing = sorted(required - set(plan))
        if missing:
            raise ValueError(f"allocator plan missing fields: {', '.join(missing)}")
        if plan["strategy"] != self.namespace:
            raise ValueError("allocator plan namespace mismatch")
        parse_ts(str(plan["created_at"]))
        parse_ts(str(plan["valid_until"]))
        plans = self.plans()
        value = dict(plan)
        value["ticker"] = str(value["ticker"]).upper()
        value["updated_at"] = str(value.get("updated_at") or utc_now())
        plans[str(value["plan_id"])] = value
        self.store.write_json("allocator_plans.json", plans)
        append_jsonl(
            self.root,
            self.log_name,
            {"event": "allocator_plan_saved", "namespace": self.namespace, "plan": value},
        )
        return value

    def replace_plan(
        self,
        prior_plan_id: str,
        replacement: dict[str, Any],
        *,
        reason: str,
        now: str,
    ) -> dict[str, Any]:
        required = {
            "plan_id",
            "strategy",
            "ticker",
            "created_at",
            "valid_until",
            "status",
            "signal",
            "snapshot",
        }
        missing = sorted(required - set(replacement))
        if missing:
            raise ValueError(f"allocator plan missing fields: {', '.join(missing)}")
        if replacement["strategy"] != self.namespace:
            raise ValueError("allocator plan namespace mismatch")
        parse_ts(str(replacement["created_at"]))
        parse_ts(str(replacement["valid_until"]))

        plans = self.plans()
        prior = plans.get(prior_plan_id)
        if prior is None:
            raise ValueError(f"allocator plan not found: {prior_plan_id}")
        value = dict(replacement)
        value["ticker"] = str(value["ticker"]).upper()
        value["updated_at"] = now
        prior["status"] = "superseded"
        prior["status_reason"] = reason
        prior["updated_at"] = now
        plans[prior_plan_id] = prior
        plans[str(value["plan_id"])] = value
        self.store.write_json("allocator_plans.json", plans)
        append_jsonl(
            self.root,
            self.log_name,
            {
                "event": "allocator_plan_replaced",
                "namespace": self.namespace,
                "prior_plan_id": prior_plan_id,
                "reason": reason,
                "decision_time": now,
                "plan": value,
            },
        )
        return value

    def plans(self) -> dict[str, dict[str, Any]]:
        raw = self.store.read_json("allocator_plans.json", {})
        return _record_mapping(raw)

    def active_plans(self, now: str) -> list[dict[str, Any]]:
        current = parse_ts(now)
        plans: list[dict[str, Any]] = []
        for value in self.plans().values():
            try:
                active = (
                    value.get("status") == "active"
                    and parse_ts(str(value["created_at"])) <= current
                    and current <= parse_ts(str(value["valid_until"]))
                )
            except (KeyError, TypeError, ValueError):
                continue
            if active:
                plans.append(value)
        return sorted(plans, key=lambda value: (str(value["created_at"]), str(value["plan_id"])))

    def set_plan_status(
        self,
        plan_id: str,
        status: str,
        *,
        reason: str | None = None,
        now: str | None = None,
    ) -> dict[str, Any] | None:
        plans = self.plans()
        plan = plans.get(plan_id)
        if plan is None:
            return None
        plan["status"] = status
        plan["status_reason"] = reason
        plan["updated_at"] = now or utc_now()
        plans[plan_id] = plan
        self.store.write_json("allocator_plans.json", plans)
        append_jsonl(
            self.root,
            self.log_name,
            {
                "event": "allocator_plan_status_changed",
                "namespace": self.namespace,
                "plan_id": plan_id,
                "status": status,
                "reason": reason,
                "decision_time": plan["updated_at"],
            },
        )
        return plan

    def record_allocation(self, allocation: dict[str, Any]) -> dict[str, Any]:
        allocation_id = str(allocation.get("allocation_id") or "")
        if not allocation_id:
            raise ValueError("allocation_id is required")
        values = self.allocations()
        record = dict(allocation)
        record.setdefault("recorded_at", utc_now())
        values[allocation_id] = record
        self.store.write_json("allocator_allocations.json", values)
        append_jsonl(
            self.root,
            self.log_name,
            {
                "event": "allocator_allocation_recorded",
                "namespace": self.namespace,
                "allocation": record,
            },
        )
        return record

    def allocations(self) -> dict[str, dict[str, Any]]:
        raw = self.store.read_json("allocator_allocations.json", {})
        return _record_mapping(raw)
