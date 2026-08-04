from __future__ import annotations

import json
from pathlib import Path

from scripts.broker.robinhood_mcp_audit import READ_ONLY_DATA_TOOLS, RobinhoodMcpCapabilityClient
from scripts.core.config import load_runtime_config
from scripts.core.models import Quote
from scripts.discovery.catalyst_pipeline import CatalystDiscoveryPipeline
from scripts.discovery.evidence_store import EvidenceSnapshotStore
from scripts.evaluation.evaluate_catalyst_strategy import evaluate_catalyst_strategy
from scripts.llm.mock_provider import MockProvider
from scripts.llm.usage_tracker import UsageTracker
from scripts.options.models import OptionContract, OptionQuote


NOW = "2026-07-06T15:00:00+00:00"
LATER = "2026-07-06T16:00:00+00:00"


class FakeDiscoveryAdapter:
    def __init__(self) -> None:
        self.now = NOW

    def collect_seed_candidates(self, decision_time, core_watchlist):
        self.now = decision_time
        assert "IONQ" not in core_watchlist
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

    def fetch_market_context(self, symbols, decision_time):
        self.now = decision_time
        quote = Quote(
            "IONQ",
            49.95,
            50.0,
            49.98,
            decision_time,
            source="fixture",
            avg_daily_volume_usd=100_000_000,
        )
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
                # This deliberately fails the deterministic baseline thresholds.
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

    def validate_instrument(self, symbol):
        return {
            "valid": symbol == "IONQ",
            "reason": "fixture exact match",
            "name": "IonQ Inc.",
            "instrument_id": "fixture-ionq",
        }

    def fetch_current_quote(self, symbol, *, average_daily_volume_usd, asset_class="us_equity"):
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


class FakeNewsAdapter:
    def _event(self, decision_time, ticker=None):
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

    def search_market_events(self, decision_time, queries):
        return [self._event(decision_time)], [
            {
                "source": "company.example",
                "source_tier": 1,
                "url": "https://company.example/press/contract",
                "retrieved_at": decision_time,
            }
        ]

    def search(self, ticker, decision_time, company_name=None):
        return [self._event(decision_time, ticker)], [
            {
                "source": "company.example",
                "source_tier": 1,
                "url": "https://company.example/press/contract",
                "retrieved_at": decision_time,
            }
        ]


class NoOptionData:
    def fetch_best_contract(self, **kwargs):
        raise AssertionError("equity catalyst proposal must not request an option contract")


class FakeBearishDiscoveryAdapter(FakeDiscoveryAdapter):
    def fetch_market_context(self, symbols, decision_time):
        context = super().fetch_market_context(symbols, decision_time)
        context["IONQ"]["technical_signals"]["price_change_1d_pct"] = -1.0
        return context


class FakeNegativeNewsAdapter(FakeNewsAdapter):
    def _event(self, decision_time, ticker=None):
        event = super()._event(decision_time, ticker)
        event["headline"] = "IonQ discloses a material contract cancellation"
        event["direction"] = "negative"
        event["url"] = "https://company.example/press/cancellation"
        return event


class FakePutOptionData:
    @staticmethod
    def fetch_best_contract(**kwargs):
        assert kwargs["option_type"] == "put"
        contract = OptionContract(
            option_id="ionq-put-50",
            chain_id="ionq-chain",
            underlying="IONQ",
            option_type="put",
            strike_price=50,
            expiration_date="2026-08-07",
            multiplier=100,
        )
        quote = OptionQuote(
            option_id=contract.option_id,
            bid=0.95,
            ask=1.0,
            mark=0.975,
            updated_at=NOW,
            source="fixture",
            delta=-0.45,
            gamma=0.04,
            theta=-0.03,
            vega=0.08,
            implied_volatility=0.25,
            volume=1000,
            open_interest=5000,
        )
        return contract, quote


def make_pipeline(paper_root):
    config = load_runtime_config(paper_root)
    tracker = UsageTracker()
    return CatalystDiscoveryPipeline(
        paper_root,
        config,
        MockProvider(tracker),
        tracker,
        discovery_adapter=FakeDiscoveryAdapter(),
        news_adapter=FakeNewsAdapter(),
        option_data=NoOptionData(),
    )


def test_catalyst_lane_discovers_outside_watchlist_without_baseline_gate(paper_root):
    result = make_pipeline(paper_root).run(NOW)
    assert result["event"] == "catalyst_discovery_complete"
    assert result["candidate_count"] == 1
    assert result["ranked_candidates"][0]["ticker"] == "IONQ"
    assert result["decisions"][0]["ticker"] == "IONQ"
    assert result["decisions"][0]["risk_approved"] is True
    assert result["decisions"][0]["final_action"] == "buy"
    assert result["paper_orders_created"] == 0
    assert result["live_order_tools_called"] is False
    assert json.loads((paper_root / "state" / "paper_orders.json").read_text(encoding="utf-8")) == {}


def test_catalyst_event_and_ticker_cooldowns_prevent_hourly_deep_repeat(paper_root):
    pipeline = make_pipeline(paper_root)
    first = pipeline.run(NOW)
    second = pipeline.run(LATER)
    assert first["model_calls"] == 5
    assert first["market_events_sent_to_model"] == 1
    assert second["model_calls"] == 2
    assert second["market_events_sent_to_model"] == 0
    assert second["decisions"] == []
    assert second["skipped_deep_research"][0]["reason"] == "ticker cooldown active and no new event"


def test_catalyst_lane_can_propose_long_put_but_still_uses_option_risk_gate(paper_root):
    config = load_runtime_config(paper_root)
    tracker = UsageTracker()
    pipeline = CatalystDiscoveryPipeline(
        paper_root,
        config,
        MockProvider(tracker),
        tracker,
        discovery_adapter=FakeBearishDiscoveryAdapter(),
        news_adapter=FakeNegativeNewsAdapter(),
        option_data=FakePutOptionData(),
    )
    result = pipeline.run(NOW)
    decision = result["decisions"][0]
    assert decision["instrument"] == "put"
    assert decision["action"] == "buy_to_open"
    assert decision["risk_approved"] is True
    assert decision["proposal"]["contract"]["option_type"] == "put"
    assert result["paper_orders_created"] == 0
    assert json.loads((paper_root / "state" / "paper_option_orders.json").read_text(encoding="utf-8")) == {}


def test_evidence_snapshot_is_immutable_and_deduplicated(paper_root):
    store = EvidenceSnapshotStore(paper_root)
    event = FakeNewsAdapter()._event(NOW, "IONQ")
    normalized = store.normalize_events([event, dict(event)], ticker="IONQ")
    assert len(normalized) == 1
    assert normalized[0]["event_fingerprint"]
    assert normalized[0]["content_hash"]
    saved = store.write_snapshot(snapshot_type="test", decision_time=NOW, payload={"events": normalized})
    path = Path(saved["path"])
    assert path.exists()
    try:
        store.write_snapshot(snapshot_type="test", decision_time=NOW, payload={"events": normalized})
    except FileExistsError:
        pass
    else:
        raise AssertionError("immutable snapshot path must not be overwritten")


def test_robinhood_discovery_allowlist_contains_no_order_mutations():
    forbidden = {
        "place_equity_order",
        "place_option_order",
        "review_equity_order",
        "review_option_order",
        "cancel_equity_order",
        "cancel_option_order",
    }
    assert forbidden.isdisjoint(READ_ONLY_DATA_TOOLS)
    assert forbidden.isdisjoint(dir(RobinhoodMcpCapabilityClient))


def test_catalyst_evaluation_reports_shadow_activity_and_cost(paper_root):
    discovery = {
        "event": "catalyst_discovery_complete",
        "candidate_count": 8,
        "paper_orders_created": 0,
    }
    decision = {
        "ticker": "IONQ",
        "instrument": "equity",
        "final_action": "buy",
        "risk_approved": True,
        "fail_closed": False,
    }
    usage = {
        "agent_name": "catalyst_ranker",
        "input_tokens": 100,
        "output_tokens": 20,
        "latency_ms": 50,
        "estimated_cost_usd": 0.001,
        "error": None,
    }
    (paper_root / "logs" / "catalyst_discovery.jsonl").write_text(json.dumps(discovery) + "\n", encoding="utf-8")
    (paper_root / "logs" / "catalyst_decisions.jsonl").write_text(json.dumps(decision) + "\n", encoding="utf-8")
    (paper_root / "logs" / "llm_usage.jsonl").write_text(json.dumps(usage) + "\n", encoding="utf-8")
    report = evaluate_catalyst_strategy(paper_root)
    assert report["discovery_cycles"] == 1
    assert report["candidate_count"] == 8
    assert report["risk_approval_rate"] == 1.0
    assert report["estimated_cost_usd"] == 0.001
    assert report["promotion_eligible"] is False
