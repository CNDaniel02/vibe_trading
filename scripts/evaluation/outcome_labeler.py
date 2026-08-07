from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

from scripts.core.audit import append_jsonl
from scripts.core.models import Quote, parse_ts
from scripts.core.state import JsonStateStore
from scripts.runtime.market_clock import UsEquityMarketClock
from scripts.strategies.technical_scoring import AdaptiveWeightStore


class CandidateOutcomeLabeler:
    """Labels point-in-time scores only after a configured future horizon exists."""

    FILENAME = "pending_candidate_outcomes.json"

    def __init__(
        self,
        root: str | Path,
        strategy_profile: dict[str, Any],
        execution_costs: dict[str, Any] | None = None,
    ) -> None:
        self.root = Path(root)
        self.profile = strategy_profile
        self.costs = execution_costs or {}
        self.store = JsonStateStore(root)
        self.horizon_minutes = int(strategy_profile.get("outcome_horizon_minutes", 60))
        self.sampling_minutes = max(
            0,
            int(strategy_profile.get("outcome_sampling_minutes", self.horizon_minutes)),
        )
        self.max_label_delay_minutes = max(
            0,
            int(strategy_profile.get("max_outcome_label_delay_minutes", 15)),
        )
        self.weights = AdaptiveWeightStore(root, strategy_profile)
        self.clock = UsEquityMarketClock()

    def register(self, decision: dict[str, Any], snapshot: dict[str, Any]) -> None:
        quote = snapshot.get("market_data", {}).get("quote", {})
        ask = float(quote.get("ask") or 0)
        if ask <= 0:
            return
        pending = self.store.read_json(self.FILENAME, {})
        snapshot_id = str(snapshot["snapshot_id"])
        if snapshot_id in pending:
            return
        decision_time = str(snapshot["decision_time"])
        decision_ts = parse_ts(decision_time)
        if self.sampling_minutes and any(
            str(observation.get("ticker", "")) == str(snapshot["ticker"])
            and str(observation.get("strategy", "")) == str(decision["strategy"])
            and timedelta(0)
            <= decision_ts - parse_ts(str(observation["decision_time"]))
            < timedelta(minutes=self.sampling_minutes)
            for observation in pending.values()
        ):
            return
        target_time = (decision_ts + timedelta(minutes=self.horizon_minutes)).isoformat()
        if not self.clock.status(target_time).is_regular:
            return
        pending[snapshot_id] = {
            "snapshot_id": snapshot_id,
            "strategy": decision["strategy"],
            "ticker": snapshot["ticker"],
            "decision_time": decision_time,
            "data_cutoff_time": snapshot["data_cutoff_time"],
            "target_time": target_time,
            "entry_ask": ask,
            "assumed_entry_price": self._adverse_price(ask, side="buy"),
            "score": float(decision["score"]),
            "action": decision["action"],
            "feature_scores": dict(decision["feature_scores"]),
        }
        self.store.write_json(self.FILENAME, pending)
        append_jsonl(self.root, "candidate_observations.jsonl", pending[snapshot_id])

    def resolve(self, quotes: dict[str, Quote], asof: str) -> list[dict[str, Any]]:
        pending = self.store.read_json(self.FILENAME, {})
        resolved: list[dict[str, Any]] = []
        remaining: dict[str, dict[str, Any]] = {}
        current_time = parse_ts(asof)
        for snapshot_id, observation in pending.items():
            quote = quotes.get(str(observation.get("ticker", "")))
            target = parse_ts(str(observation["target_time"]))
            if not self.clock.status(target.isoformat()).is_regular:
                self._record_expired(observation, asof, "target time is outside regular market hours")
                continue
            if quote is None or current_time < target or parse_ts(quote.asof) < target:
                if current_time - parse_ts(str(observation["decision_time"])) <= timedelta(days=7):
                    remaining[snapshot_id] = observation
                continue
            if parse_ts(quote.asof) - target > timedelta(
                minutes=self.max_label_delay_minutes
            ):
                self._record_expired(
                    observation,
                    asof,
                    "first available quote arrived after the label-delay limit",
                )
                continue
            if parse_ts(quote.asof) > current_time + timedelta(seconds=1):
                remaining[snapshot_id] = observation
                continue
            entry_price = float(
                observation.get(
                    "assumed_entry_price",
                    self._adverse_price(float(observation["entry_ask"]), side="buy"),
                )
            )
            exit_price = self._adverse_price(quote.bid, side="sell")
            round_trip_commission = 2 * float(self.costs.get("commission_per_order_usd", 0))
            net_return = (exit_price - entry_price - round_trip_commission) / entry_price
            label = 1.0 if net_return > 0 else 0.0
            weight_state = self.weights.observe(observation["feature_scores"], label)
            record = {
                **observation,
                "resolved_at": asof,
                "exit_quote_asof": quote.asof,
                "exit_bid": quote.bid,
                "assumed_exit_price": exit_price,
                "round_trip_commission_usd": round_trip_commission,
                "net_return_pct": round(net_return * 100, 6),
                "profitable_after_spread": bool(label),
                "weight_state_after": weight_state,
            }
            append_jsonl(self.root, "candidate_outcomes.jsonl", record)
            resolved.append(record)
        self.store.write_json(self.FILENAME, remaining)
        return resolved

    def _record_expired(
        self,
        observation: dict[str, Any],
        asof: str,
        reason: str,
    ) -> None:
        append_jsonl(
            self.root,
            "audit.jsonl",
            {
                "event": "candidate_outcome_expired",
                "snapshot_id": observation.get("snapshot_id"),
                "ticker": observation.get("ticker"),
                "target_time": observation.get("target_time"),
                "expired_at": asof,
                "reason": reason,
            },
        )

    def _adverse_price(self, reference: float, *, side: str) -> float:
        slippage = max(
            reference * float(self.costs.get("slippage_bps", 0)) / 10_000,
            float(self.costs.get("minimum_slippage_usd", 0)),
        )
        return reference + slippage if side == "buy" else max(0.0, reference - slippage)
