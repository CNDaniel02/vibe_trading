from __future__ import annotations

import json
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from scripts.adapters.vibe_market_data_adapter import MarketBar
from scripts.core.models import Quote
from scripts.evaluation.calculate_metrics import calculate_metrics
from scripts.orchestrator.forward_paper_service import ForwardPaperService


DRY_RUN_TIME = "2026-07-13T15:00:00Z"


class _SyntheticVibe:
    def fetch_lookback(self, symbols: list[str], decision_time: str) -> dict[str, list[MarketBar]]:
        del decision_time
        start = datetime(2026, 5, 25, tzinfo=timezone.utc)
        output: dict[str, list[MarketBar]] = {}
        for symbol in symbols:
            slope = 0.35 if symbol == "AAPL" else 0.05
            bars = []
            for index in range(45):
                close = 95 + slope * index
                timestamp = (start + timedelta(days=index)).isoformat()
                bars.append(MarketBar(symbol, timestamp, close - 0.2, close + 0.3, close - 0.4, close, 1_000_000, "fixture:vibe"))
            output[symbol] = bars
        return output

    @staticmethod
    def average_daily_volume_usd(bars: list[MarketBar], cutoff_time: str) -> float:
        del bars, cutoff_time
        return 100_000_000


class _SyntheticQuotes:
    @staticmethod
    def fetch_quotes(symbols: list[str], **_: Any) -> dict[str, Quote]:
        available = {
            "AAPL": Quote(
                "AAPL",
                111.00,
                111.04,
                111.02,
                "2026-07-13T14:59:55Z",
                source="fixture:alpaca-iex",
                avg_daily_volume_usd=100_000_000,
                session_volume=10_000_000,
                previous_close=110.00,
            ),
            "SPY": Quote(
                "SPY",
                100.00,
                100.02,
                100.01,
                "2026-07-13T14:59:55Z",
                source="fixture:alpaca-iex",
                avg_daily_volume_usd=100_000_000,
                asset_class="us_etf",
                session_volume=10_000_000,
                previous_close=99.90,
            ),
        }
        return {symbol: available[symbol] for symbol in symbols if symbol in available}


class _SyntheticNews:
    @staticmethod
    def search(ticker: str, decision_time: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        del decision_time
        event = {
            "headline": f"{ticker} raises full-year guidance in a company release",
            "published_at": "2026-07-13T14:30:00Z",
            "first_seen_at": "2026-07-13T14:30:02Z",
            "source": "Company IR",
            "source_tier": 1,
            "ticker_relevance": 1.0,
            "direction": "positive",
            "novelty": 0.95,
            "already_priced_in": False,
            "confidence": 0.9,
        }
        return [event], [{"source": "Company IR", "source_tier": 1}]


def run_dry_run(project_root: str | Path) -> dict[str, Any]:
    project_root = Path(project_root).resolve()
    with tempfile.TemporaryDirectory(prefix="auto-trading-dry-run-") as temp:
        root = Path(temp)
        shutil.copytree(project_root / "config", root / "config")
        paper_path = root / "config" / "paper_mode.yaml"
        paper_config = yaml.safe_load(paper_path.read_text(encoding="utf-8"))
        paper_config.setdefault("strategy_lines", {})["options"] = False
        paper_path.write_text(yaml.safe_dump(paper_config, sort_keys=False), encoding="utf-8")
        llm_path = root / "config" / "llm.yaml"
        llm_config = yaml.safe_load(llm_path.read_text(encoding="utf-8"))
        llm_config["provider"] = "mock"
        llm_config["usage_log"] = "logs/evals/dry_run_usage.jsonl"
        llm_path.write_text(yaml.safe_dump(llm_config, sort_keys=False), encoding="utf-8")
        (root / "state").mkdir()
        (root / "logs").mkdir()
        service = ForwardPaperService(root)
        service.vibe = _SyntheticVibe()  # type: ignore[assignment]
        service.quote_adapter = _SyntheticQuotes()  # type: ignore[assignment]
        service.news_adapter = _SyntheticNews()  # type: ignore[assignment]
        result = service.run_once(DRY_RUN_TIME)
        account = json.loads((root / "state" / "paper_account.json").read_text(encoding="utf-8"))
        positions = json.loads((root / "state" / "paper_positions.json").read_text(encoding="utf-8"))
        report = {
            "dry_run": True,
            "used_network": False,
            "used_live_order_tools": False,
            "used_virtual_state": True,
            "result": result,
            "paper_account": account,
            "paper_positions": positions,
            "metrics": calculate_metrics(root),
        }
    output = project_root / "logs" / "forward_pipeline_dry_run.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


if __name__ == "__main__":
    project = Path(__file__).resolve().parents[2]
    print(json.dumps(run_dry_run(project), indent=2, sort_keys=True))
