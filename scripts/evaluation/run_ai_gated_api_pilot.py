from __future__ import annotations

import argparse
import json
from pathlib import Path

from scripts.agents.ai_gated_investment_team import AiGatedInvestmentTeam
from scripts.core.config import assert_paper_mode, load_runtime_config
from scripts.llm import build_provider


PILOT_TIME = "2026-07-27T15:00:00+00:00"


def run_pilot(root: str | Path) -> dict:
    root = Path(root).resolve()
    config = load_runtime_config(root)
    assert_paper_mode(config)
    provider, tracker = build_provider(config["llm"], root)
    team = AiGatedInvestmentTeam(config, provider, tracker)
    candidates = [
        {
            "ticker": "AAPL",
            "eligible": True,
            "pre_score": 0.78,
            "technical_direction": "bullish",
            "market_context": {
                "quote": {
                    "symbol": "AAPL",
                    "bid": 210.10,
                    "ask": 210.14,
                    "last": 210.12,
                    "asof": PILOT_TIME,
                    "source": "immutable_api_pilot_fixture",
                    "avg_daily_volume_usd": 10_000_000_000,
                    "asset_class": "us_equity",
                },
                "technical_signals": {
                    "relative_strength_20d": 3.2,
                    "price_change_1d_pct": 1.1,
                    "price_change_5d_pct": 2.8,
                    "volume_ratio": 1.4,
                    "chase_score": 0.25,
                },
                "fundamentals": {"market_cap": 3_000_000_000_000},
            },
            "events": [
                {
                    "headline": "Company raises full-year guidance",
                    "published_at": "2026-07-27T14:30:00+00:00",
                    "event_at": "2026-07-27T14:25:00+00:00",
                    "source": "company.example",
                    "source_tier": 1,
                    "url": "https://company.example/investors/guidance",
                    "highlights": ["The supplied fixture states that full-year guidance increased."],
                }
            ],
        }
    ]
    ranking = team.rank(
        snapshot_id="ai-gated-api-pilot",
        decision_time=PILOT_TIME,
        candidates=candidates,
    )
    ranked = ranking.get("ranked_candidates", [])
    selected = ranked[0] if ranked else {
        "ticker": "AAPL",
        "score": 0,
        "direction": "unclear",
        "instrument_preference": "none",
    }
    snapshot = {
        "snapshot_id": "ai-gated-api-pilot-AAPL",
        "decision_time": PILOT_TIME,
        "data_cutoff_time": PILOT_TIME,
        "ticker": "AAPL",
        "market_session": "regular",
        "market_data": {
            "quote": candidates[0]["market_context"]["quote"],
            "fundamentals": candidates[0]["market_context"]["fundamentals"],
            "market_regime": "neutral",
            "binary_event_within_days": 30,
            "has_position": False,
            "paper_sleeve": "api_pilot_no_execution",
        },
        "technical_signals": candidates[0]["market_context"]["technical_signals"],
        "available_news": [
            {
                **candidates[0]["events"][0],
                "first_seen_at": "2026-07-27T14:31:00+00:00",
                "retrieved_at": "2026-07-27T14:31:00+00:00",
                "ticker_relevance": 1.0,
                "direction": "positive",
                "novelty": 0.9,
                "already_priced_in": False,
                "confidence": 0.9,
            }
        ],
        "source_metadata": [
            {
                "source": "company.example",
                "source_tier": 1,
                "url": "https://company.example/investors/guidance",
                "retrieved_at": "2026-07-27T14:31:00+00:00",
            }
        ],
    }
    analysis = team.analyze(snapshot, selected)
    decision = analysis["decision"]
    return {
        "event": "ai_gated_api_pilot_complete",
        "fixture_only": True,
        "market_data_calls": 0,
        "exa_calls": 0,
        "model_calls": len(tracker.records),
        "usage": tracker.summary(),
        "ranked_ticker": selected.get("ticker"),
        "rank_score": selected.get("score"),
        "decision": {
            "action": decision.get("action"),
            "instrument": decision.get("instrument"),
            "confidence": decision.get("confidence"),
            "no_trade_reason": decision.get("no_trade_reason"),
        },
        "challenge_veto": bool((analysis.get("challenge") or {}).get("veto_recommended", False)),
        "fail_closed": bool(analysis.get("fail_closed", False)),
        "paper_order_created": False,
        "live_order_tools_called": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[2]))
    args = parser.parse_args()
    print(json.dumps(run_pilot(args.root), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
