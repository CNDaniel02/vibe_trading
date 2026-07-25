from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

import yaml

from scripts.core.config import load_runtime_config
from scripts.core.models import Quote
from scripts.discovery.catalyst_pipeline import CatalystDiscoveryPipeline
from scripts.llm.mock_provider import MockProvider
from scripts.llm.usage_tracker import UsageTracker


DRY_RUN_TIME = "2026-07-06T15:00:00+00:00"


class _DiscoveryFixture:
    def __init__(self) -> None:
        self.now = DRY_RUN_TIME

    def collect_seed_candidates(self, decision_time: str, core_watchlist: list[str]) -> list[dict[str, Any]]:
        del core_watchlist
        self.now = decision_time
        return [
            {
                "ticker": "IONQ",
                "sources": ["earnings_calendar"],
                "source_details": [
                    {
                        "source": "earnings_calendar",
                        "detail": {"eps_surprise_ratio": 0.4},
                    }
                ],
            }
        ]

    def fetch_market_context(self, symbols: list[str], decision_time: str) -> dict[str, dict[str, Any]]:
        del symbols
        self.now = decision_time
        quote = self.fetch_current_quote("IONQ", average_daily_volume_usd=100_000_000)
        return {
            "IONQ": {
                "ticker": "IONQ",
                "eligible": True,
                "quote": quote.to_dict(),
                "fundamentals": {
                    "market_cap": 10_000_000_000,
                    "average_daily_volume_usd": 100_000_000,
                    "volume_ratio": 0.5,
                },
                "technical_signals": {
                    "price_change_1d_pct": 0.2,
                    "price_change_5d_pct": -1.0,
                    "price_change_20d_pct": -2.0,
                    "relative_strength_20d": -2.0,
                    "volume_ratio": 0.5,
                    "spread_bps": 10.0,
                },
                "source": "fixture",
                "data_cutoff_time": decision_time,
            }
        }

    @staticmethod
    def validate_instrument(symbol: str) -> dict[str, Any]:
        return {
            "valid": symbol == "IONQ",
            "reason": "fixture exact match",
            "name": "IonQ Inc.",
            "instrument_id": "fixture-ionq",
        }

    def fetch_current_quote(
        self,
        symbol: str,
        *,
        average_daily_volume_usd: float | None,
        asset_class: str = "us_equity",
    ) -> Quote:
        return Quote(
            symbol,
            49.95,
            50.0,
            49.98,
            self.now,
            source="fixture",
            avg_daily_volume_usd=average_daily_volume_usd,
            asset_class=asset_class,
        )


class _NewsFixture:
    @staticmethod
    def _event(decision_time: str, ticker: str | None = None) -> dict[str, Any]:
        return {
            "ticker": ticker,
            "headline": "IonQ announces a material government contract",
            "published_at": decision_time,
            "event_at": decision_time,
            "first_seen_at": decision_time,
            "retrieved_at": decision_time,
            "source": "company.example",
            "source_tier": 1,
            "ticker_relevance": 1.0,
            "direction": "positive",
            "novelty": 1.0,
            "already_priced_in": False,
            "confidence": 0.9,
            "url": "https://company.example/press/contract",
            "highlights": ["The company reported a signed material contract."],
        }

    def search_market_events(self, decision_time: str, queries: list[str]):
        del queries
        return [self._event(decision_time)], [
            {"source": "company.example", "source_tier": 1, "url": "https://company.example/press/contract"}
        ]

    def search(self, ticker: str, decision_time: str, company_name: str | None = None):
        del company_name
        return [self._event(decision_time, ticker)], [
            {"source": "company.example", "source_tier": 1, "url": "https://company.example/press/contract"}
        ]


class _NoOptionFixture:
    @staticmethod
    def fetch_best_contract(**_: Any):
        raise AssertionError("equity fixture must not enter option selection")


def run_catalyst_dry_run(project_root: str | Path) -> dict[str, Any]:
    project_root = Path(project_root).resolve()
    with tempfile.TemporaryDirectory(prefix="catalyst-paper-dry-run-") as temporary:
        root = Path(temporary)
        shutil.copytree(project_root / "config", root / "config")
        llm_path = root / "config" / "llm.yaml"
        llm_config = yaml.safe_load(llm_path.read_text(encoding="utf-8"))
        llm_config["provider"] = "mock"
        llm_path.write_text(yaml.safe_dump(llm_config, sort_keys=False), encoding="utf-8")
        (root / "state").mkdir()
        (root / "logs").mkdir()
        config = load_runtime_config(root)
        tracker = UsageTracker()
        pipeline = CatalystDiscoveryPipeline(
            root,
            config,
            MockProvider(tracker),
            tracker,
            discovery_adapter=_DiscoveryFixture(),
            news_adapter=_NewsFixture(),
            option_data=_NoOptionFixture(),
        )
        result = pipeline.run(DRY_RUN_TIME)
        report = {
            "dry_run": True,
            "used_network": False,
            "used_live_order_tools": False,
            "used_virtual_state": True,
            "result": result,
            "paper_orders": json.loads((root / "state" / "paper_orders.json").read_text(encoding="utf-8")),
            "option_orders": json.loads((root / "state" / "paper_option_orders.json").read_text(encoding="utf-8")),
        }
    output = project_root / "logs" / "catalyst_pipeline_dry_run.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


if __name__ == "__main__":
    print(json.dumps(run_catalyst_dry_run(Path(__file__).resolve().parents[2]), indent=2, sort_keys=True))
