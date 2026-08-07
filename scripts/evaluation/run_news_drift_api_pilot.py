from __future__ import annotations

import argparse
import json
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from typing import Any

from scripts.agents.news_drift_headline_agent import NewsDriftHeadlineAgent
from scripts.core.config import load_runtime_config
from scripts.core.models import parse_ts, utc_now
from scripts.llm import build_provider
from scripts.llm.base_provider import LLMProvider
from scripts.llm.usage_tracker import UsageTracker


def run_pilot(
    root: str | Path,
    *,
    provider: LLMProvider | None = None,
    tracker: UsageTracker | None = None,
) -> dict[str, Any]:
    root = Path(root).resolve()
    config = deepcopy(load_runtime_config(root))
    if provider is None or tracker is None:
        provider, tracker = build_provider(config["llm"], root)
    now = utc_now()
    published = (parse_ts(now) - timedelta(minutes=2)).isoformat()
    agent = NewsDriftHeadlineAgent(config, provider, tracker)
    before = len(tracker.records)
    output = agent.analyze(
        snapshot_id="news_drift_api_pilot",
        decision_time=now,
        events=[
            {
                "headline": "Example Corp raises full-year guidance after a material contract win",
                "published_at": published,
                "source": "example.com",
                "source_tier": 3,
                "ticker": "EXMPL",
                "company_name": "Example Corp",
            },
            {
                "headline": "Sample Inc cuts guidance and announces a product recall",
                "published_at": published,
                "source": "example.org",
                "source_tier": 3,
                "ticker": "SMPL",
                "company_name": "Sample Inc",
            },
        ],
        recent_events=[],
    )
    records = tracker.records[before:]
    return {
        "event": "news_drift_api_pilot_complete",
        "model_calls": len(records),
        "signals": output["signals"],
        "usage": {
            "input_tokens": sum(item.input_tokens for item in records),
            "output_tokens": sum(item.output_tokens for item in records),
            "latency_ms": round(sum(item.latency_ms for item in records), 3),
            "estimated_cost_usd": round(sum(item.estimated_cost_usd or 0 for item in records), 8),
            "errors": sum(bool(item.error) for item in records),
            "retries": sum(item.retries for item in records),
        },
        "market_data_calls": 0,
        "paper_orders_created": 0,
        "live_order_tools_called": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[2]))
    args = parser.parse_args()
    print(json.dumps(run_pilot(args.root), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
