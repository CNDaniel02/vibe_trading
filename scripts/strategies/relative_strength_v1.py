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
    action = "buy" if regime["eligible"] and technical["candidate"] else "no_trade"
    return {
        "strategy": STRATEGY_NAME,
        "snapshot_id": snapshot["snapshot_id"],
        "ticker": snapshot["ticker"],
        "action": action,
        "regime": regime,
        "technical": technical,
    }
