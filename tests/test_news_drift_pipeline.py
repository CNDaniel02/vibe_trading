from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from typing import Any

from scripts.core.config import load_runtime_config
from scripts.core.models import Quote, parse_ts
from scripts.llm.base_provider import ProviderRequest
from scripts.llm.mock_provider import MockProvider
from scripts.llm.usage_tracker import UsageTracker
from scripts.news_drift.event_store import NewsEventStore
from scripts.news_drift.pipeline import NewsDriftPipeline
from scripts.orchestrator import forward_paper_service


NOW = "2026-08-04T14:31:00+00:00"


class CapturingProvider(MockProvider):
    def __init__(self, tracker: UsageTracker) -> None:
        super().__init__(tracker)
        self.requests: list[ProviderRequest] = []

    def generate(self, request: ProviderRequest):
        self.requests.append(request)
        return super().generate(request)


class FakeNews:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.events = events
        self.search_calls = 0

    def readiness(self) -> dict[str, Any]:
        return {"ready": True, "enabled": True, "missing_env": []}

    def search_market_events(self, decision_time: str, queries: list[str]):
        self.search_calls += 1
        return deepcopy(self.events), [{"source": "sec.gov", "source_tier": 1}]


class FakeDiscovery:
    def __init__(self, quote_asof: str = NOW) -> None:
        self.quote_asof = quote_asof
        self.quote_calls = 0

    def readiness(self) -> dict[str, Any]:
        return {"ready": True, "reason": "fixture"}

    def validate_instrument(self, symbol: str) -> dict[str, Any]:
        return {"valid": symbol == "GOOD", "reason": "fixture", "name": "Good Corp"}

    def fetch_market_context(self, symbols: list[str], decision_time: str) -> dict[str, dict[str, Any]]:
        if "GOOD" not in symbols:
            return {}
        quote = Quote(
            symbol="GOOD",
            bid=100.95,
            ask=101.05,
            last=101.0,
            asof=self.quote_asof,
            source="fixture",
            avg_daily_volume_usd=50_000_000,
            previous_close=100.0,
        )
        return {
            "GOOD": {
                "ticker": "GOOD",
                "eligible": True,
                "quote": quote.to_dict(),
                "fundamentals": {
                    "market_cap": 5_000_000_000,
                    "average_daily_volume_usd": 50_000_000,
                },
                "technical_signals": {},
                "data_cutoff_time": decision_time,
            }
        }

    def fetch_intraday_bars(self, symbols: list[str], start_time: str, end_time: str):
        return {
            "GOOD": [
                {
                    "begins_at": "2026-08-04T14:20:00+00:00",
                    "close_price": 100.0,
                    "interpolated": False,
                },
                {
                    "begins_at": "2026-08-04T14:30:00+00:00",
                    "close_price": 200.0,
                    "interpolated": False,
                },
            ]
        }

    def fetch_current_quote(self, symbol: str, *, average_daily_volume_usd: float | None):
        self.quote_calls += 1
        return Quote(
            symbol=symbol,
            bid=102.0,
            ask=102.1,
            last=102.05,
            asof=self.quote_asof,
            source="fixture",
            avg_daily_volume_usd=average_daily_volume_usd,
            previous_close=100.0,
        )


def _event() -> dict[str, Any]:
    return {
        "headline": "Good Corp raises guidance after contract win",
        "ticker": "GOOD",
        "published_at": "2026-08-04T14:30:00+00:00",
        "first_seen_at": "2026-08-04T14:30:20+00:00",
        "retrieved_at": "2026-08-04T14:30:20+00:00",
        "source": "sec.gov",
        "source_tier": 1,
        "url": "https://sec.gov/good/1",
        "highlights": ["Guidance increased."],
    }


def _pipeline(
    tmp_path: Path,
    *,
    quote_asof: str = NOW,
    events: list[dict[str, Any]] | None = None,
):
    config = deepcopy(load_runtime_config())
    config["llm"]["provider"] = "mock"
    config["strategies"]["llm_news_drift_v1"]["event_cooldown_hours"] = 24
    tracker = UsageTracker()
    provider = CapturingProvider(tracker)
    discovery = FakeDiscovery(quote_asof)
    pipeline = NewsDriftPipeline(
        tmp_path,
        config,
        provider,
        tracker,
        discovery_adapter=discovery,
        news_adapter=FakeNews(events or [_event()]),
    )
    return pipeline, provider, discovery


def test_news_first_shadow_pipeline_never_exposes_price_to_llm_or_creates_order(tmp_path):
    pipeline, provider, _ = _pipeline(tmp_path)

    result = pipeline.run(NOW)

    assert result["event"] == "news_drift_complete"
    assert result["model_calls"] == 1
    assert len(result["proposals"]) == 1
    assert result["paper_orders_created"] == 0
    assert result["live_order_tools_called"] is False
    assert not hasattr(pipeline, "broker")
    assert result["proposals"][0]["quantity"] == 4.945
    payload = provider.requests[0].input_payload
    serialized = str(payload).lower()
    assert "market_data" not in payload
    assert "quote" not in serialized
    assert "price" not in serialized
    assert not (tmp_path / "state" / "paper_orders.json").exists()
    assert (tmp_path / "state" / "news_events.sqlite").exists()


def test_same_raw_event_is_not_sent_to_model_twice(tmp_path):
    pipeline, provider, _ = _pipeline(tmp_path)

    first = pipeline.run(NOW)
    restarted, restarted_provider, _ = _pipeline(tmp_path)
    second = restarted.run("2026-08-04T14:32:00+00:00")

    assert first["model_calls"] == 1
    assert second["model_calls"] == 0
    assert len(provider.requests) == 1
    assert restarted_provider.requests == []
    with NewsEventStore(tmp_path / "state" / "news_events.sqlite") as store:
        assert len(store.list_events()) == 1


def test_discovery_is_throttled_while_minute_labels_keep_running(tmp_path):
    pipeline, _, _ = _pipeline(tmp_path)

    pipeline.run(NOW)
    second = pipeline.run("2026-08-04T14:32:00+00:00")

    assert pipeline.news.search_calls == 1
    assert second["event"] == "news_drift_idle"
    assert second["reason"] == "discovery cooldown active"


def test_date_precision_uses_first_seen_as_conservative_event_time(tmp_path):
    event = {
        **_event(),
        "published_at": "2026-08-04T00:00:00+00:00",
        "published_at_precision": "date",
    }
    pipeline, _, _ = _pipeline(tmp_path, events=[event])

    result = pipeline.run(NOW)

    assert len(result["proposals"]) == 1
    assert result["screened"][0]["event_time_basis"] == "first_seen_at_for_date_precision"


def test_premarket_stale_signal_is_rechecked_after_open_without_second_model_call(tmp_path):
    event = {
        **_event(),
        "published_at": "2026-08-04T12:55:00+00:00",
        "first_seen_at": "2026-08-04T13:00:00+00:00",
        "retrieved_at": "2026-08-04T13:00:00+00:00",
    }
    pipeline, provider, discovery = _pipeline(
        tmp_path,
        quote_asof="2026-08-04T12:55:00+00:00",
        events=[event],
    )
    pipeline.profile["discovery_interval_seconds"] = 3600

    premarket = pipeline.run("2026-08-04T13:00:00+00:00")
    discovery.quote_asof = "2026-08-04T13:31:00+00:00"
    after_open = pipeline.run("2026-08-04T13:31:00+00:00")

    assert premarket["proposals"] == []
    assert "stale quote" in premarket["screened"][0]["reason"]
    assert len(after_open["proposals"]) == 1
    assert len(provider.requests) == 1
    assert pipeline.news.search_calls == 1


def test_stale_quote_fails_closed_without_shadow_proposal(tmp_path):
    pipeline, _, _ = _pipeline(tmp_path, quote_asof="2026-08-04T14:20:00+00:00")

    result = pipeline.run(NOW)

    assert result["proposals"] == []
    assert result["screened"][0]["tradable"] is False
    assert "stale quote" in result["screened"][0]["reason"]


def test_reference_price_uses_only_bar_completed_before_event():
    quote = Quote(
        symbol="GOOD",
        bid=100,
        ask=101,
        last=100.5,
        asof=NOW,
        previous_close=99,
    )
    bars = [
        {"begins_at": "2026-08-04T14:20:00+00:00", "close_price": 100},
        {"begins_at": "2026-08-04T14:29:00+00:00", "close_price": 500},
    ]
    signal = {"published_at": "2026-08-04T14:30:00+00:00"}

    value = NewsDriftPipeline._reference_price(signal, bars, quote, NOW)

    assert value == 100


def test_due_label_applies_bid_and_adverse_slippage(tmp_path):
    pipeline, _, discovery = _pipeline(tmp_path)
    result = pipeline.run(NOW)
    target = result["proposals"][0]["label_targets"]["plus_1m"]
    discovery.quote_asof = target

    labels = pipeline.resolve_due_labels(target)

    assert labels == {"due": 1, "recorded": 1, "deferred": 0}
    with NewsEventStore(tmp_path / "state" / "news_events.sqlite") as store:
        row = store.connection.execute("SELECT * FROM outcome_labels").fetchone()
        payload = __import__("json").loads(row["payload_json"])
    assert payload["exit_price"] < payload["quote"]["bid"]
    assert payload["net_return_pct"] < payload["gross_return_pct"]
    assert discovery.quote_calls == 1


def test_label_does_not_use_quote_observed_before_target(tmp_path):
    pipeline, _, discovery = _pipeline(tmp_path)
    proposal = pipeline.run(NOW)["proposals"][0]
    target = proposal["label_targets"]["plus_1m"]
    discovery.quote_asof = (parse_ts(target) - timedelta(seconds=1)).isoformat()

    labels = pipeline.resolve_due_labels(target)

    assert labels == {"due": 1, "recorded": 0, "deferred": 1}


def test_news_search_is_disabled_outside_bounded_premarket_window(tmp_path):
    pipeline, provider, _ = _pipeline(tmp_path)

    result = pipeline.run("2026-08-04T08:00:00+00:00")

    assert result["event"] == "news_drift_skipped"
    assert "discovery window" in result["reason"]
    assert provider.requests == []


def test_standalone_news_drift_entrypoint_does_not_construct_paper_broker(tmp_path, monkeypatch):
    config = deepcopy(load_runtime_config())
    tracker = UsageTracker()

    class StubPipeline:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, now):
            return {
                "event": "news_drift_complete",
                "signals": [],
                "proposals": [],
                "paper_orders_created": 0,
                "live_order_tools_called": False,
            }

    monkeypatch.setattr(forward_paper_service, "load_runtime_config", lambda root: config)
    monkeypatch.setattr(forward_paper_service, "build_provider", lambda llm, root: (MockProvider(tracker), tracker))
    monkeypatch.setattr(forward_paper_service, "NewsDriftPipeline", StubPipeline)
    monkeypatch.setattr(forward_paper_service, "ExaNewsAdapter", lambda config: object())
    monkeypatch.setattr(
        forward_paper_service,
        "PaperBroker",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("paper broker constructed")),
    )

    result = forward_paper_service.run_news_drift_once(tmp_path, NOW)

    assert result["paper_orders_created"] == 0
    assert not (tmp_path / "state" / "paper_account.json").exists()
