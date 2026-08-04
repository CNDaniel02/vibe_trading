from __future__ import annotations

from typing import Any

from scripts.agents.deterministic_agents import run_regime_agent, run_technical_agent
from scripts.core.models import Quote
from scripts.research.simple_research import pick_first_valid_candidate


STRATEGY_NAME = "relative_strength_v1"


def select_paper_candidate(quotes: dict[str, Quote], watchlist: list[str]) -> dict[str, Any] | None:
    candidate = pick_first_valid_candidate(quotes, watchlist)
    if candidate:
        candidate["strategy"] = STRATEGY_NAME
    return candidate


def decide_snapshot(snapshot: dict[str, Any], runtime_config: dict[str, Any]) -> dict[str, Any]:
    regime = run_regime_agent(snapshot)
    technical = run_technical_agent(snapshot, runtime_config)
    profile = runtime_config.get("strategies", {}).get("relative_strength_v1", {})
    chase_allowed = technical["chase_score"] <= float(profile.get("max_chase_score", 1))
    has_position = bool(snapshot.get("market_data", {}).get("has_position", False))
    action = "buy" if regime["eligible"] and technical["candidate"] and chase_allowed and not has_position else "no_trade"
    technical["chase_allowed"] = chase_allowed
    technical["has_position"] = has_position
    if has_position:
        technical["reasons"].append("existing position is managed by the exit pipeline")
    return {
        "strategy": STRATEGY_NAME,
        "snapshot_id": snapshot["snapshot_id"],
        "ticker": snapshot["ticker"],
        "action": action,
        "regime": regime,
        "technical": technical,
    }
