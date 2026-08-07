from __future__ import annotations

from pathlib import Path
from typing import Any

from scripts.agents.deterministic_agents import quote_from_snapshot
from scripts.risk.risk_gate import validate_quote
from scripts.strategies.technical_scoring import AdaptiveWeightStore, directional_feature_scores, weighted_score


STRATEGY_NAME = "weighted_relative_strength_v2"


def decide_snapshot(
    snapshot: dict[str, Any],
    runtime_config: dict[str, Any],
    root: str | Path,
) -> dict[str, Any]:
    profile = runtime_config.get("strategies", {}).get(STRATEGY_NAME, {})
    quote = quote_from_snapshot(snapshot)
    quote_check = validate_quote(
        quote,
        snapshot["decision_time"],
        int(runtime_config["paper"].get("quote_stale_after_seconds", 60)),
        runtime_config["universe"],
    )
    has_position = bool(snapshot.get("market_data", {}).get("has_position", False))
    market_session = snapshot.get("market_session")
    chase = float(snapshot.get("technical_signals", {}).get("chase_score") or 0)
    hard_max_chase = float(profile.get("hard_max_chase_score", 0.95))
    hard_reasons: list[str] = []
    if not quote_check.approved:
        hard_reasons.append(quote_check.reason)
    if market_session != "regular":
        hard_reasons.append("outside regular market session")
    if has_position:
        hard_reasons.append("existing position is managed by the exit pipeline")
    if not bool(snapshot.get("market_data", {}).get("history_fresh", False)):
        hard_reasons.append("completed OHLCV history is stale")
    if chase > hard_max_chase:
        hard_reasons.append(f"extreme chase risk above {hard_max_chase:g}")
    event_days = int(
        snapshot.get("market_data", {}).get("binary_event_within_days", 99)
    )
    reject_event_days = int(
        profile.get("reject_binary_event_within_days", -1)
    )
    if reject_event_days >= 0 and event_days <= reject_event_days:
        hard_reasons.append(
            "binary earnings event inside equity exclusion window"
        )

    feature_scores = directional_feature_scores(snapshot, "bullish")
    weight_state = AdaptiveWeightStore(root, profile).current()
    score = weighted_score(feature_scores, weight_state["weights"])
    minimum_score = float(profile.get("minimum_entry_score", 0.56))
    action = "buy" if not hard_reasons and score >= minimum_score else "no_trade"
    reasons = list(hard_reasons)
    if score < minimum_score:
        reasons.append(f"weighted technical score {score:.3f} below {minimum_score:.3f}")
    return {
        "strategy": STRATEGY_NAME,
        "snapshot_id": snapshot["snapshot_id"],
        "ticker": snapshot["ticker"],
        "action": action,
        "score": score,
        "minimum_entry_score": minimum_score,
        "feature_scores": feature_scores,
        "weight_state": weight_state,
        "hard_gate_passed": not hard_reasons,
        "reasons": reasons,
        "technical": {
            **snapshot.get("technical_signals", {}),
            "weighted_score": score,
            "weights": weight_state["weights"],
        },
    }
