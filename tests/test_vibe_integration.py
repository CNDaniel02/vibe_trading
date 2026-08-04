from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

import scripts.orchestrator.forward_paper_service as forward_service_module
from scripts.adapters.alpaca_market_data_adapter import AlpacaMarketDataAdapter
from scripts.adapters.exa_news_adapter import ExaNewsAdapter
from scripts.adapters.robinhood_mcp_market_data_adapter import RobinhoodMcpMarketDataAdapter
from scripts.adapters.vibe_market_data_adapter import MarketBar
from scripts.adapters.vibe_research_swarm_adapter import VibeResearchSwarmAdapter
from scripts.adapters.vibe_runtime import VibeRuntime
from scripts.core.config import load_runtime_config
from scripts.core.models import Position, Quote, parse_ts
from scripts.exit.evaluate_exit import evaluate_position_exit
from scripts.orchestrator.dry_run_forward_pipeline import _SyntheticNews, _SyntheticQuotes, _SyntheticVibe, run_dry_run
from scripts.orchestrator.forward_paper_service import ForwardPaperService, _cycle_summary
from scripts.replay.vibe_replay_run_manager import VibeReplayRunManager
from scripts.runtime.market_clock import UsEquityMarketClock
from scripts.simulation.paper_broker import PaperBroker
from scripts.strategies.relative_strength_v1 import decide_snapshot


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


def test_vibe_runtime_is_pinned_and_clean() -> None:
    config = load_runtime_config(ROOT)
    status = VibeRuntime(ROOT, config["integrations"]["vibe"]).status()
    assert status.ready, status.reason
    assert status.actual_commit == status.expected_commit
    assert status.clean_worktree


def test_vibe_readonly_swarm_preset_passes_contract() -> None:
    config = load_runtime_config(ROOT)
    result = VibeResearchSwarmAdapter(ROOT, config["integrations"]["vibe"]).inspect()
    assert result["ok"] is True
    assert result["errors"] == []
    assert not any(fragment in tool for tool in result["allowed_tools"] for fragment in ("order", "trade", "broker", "account", "position"))


def test_external_adapters_expose_no_order_methods() -> None:
    config = load_runtime_config(ROOT)["integrations"]
    adapters = [
        AlpacaMarketDataAdapter(config["forward_data"]["alpaca"]),
        ExaNewsAdapter(config["forward_data"]["exa"]),
        RobinhoodMcpMarketDataAdapter(config["robinhood_mcp"]),
        VibeResearchSwarmAdapter(ROOT, config["vibe"]),
    ]
    forbidden = {"place_order", "submit_order", "cancel_order", "create_order"}
    for adapter in adapters:
        assert forbidden.isdisjoint(dir(adapter))


def test_alpaca_snapshot_maps_real_bid_and_ask(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.loads((FIXTURES / "alpaca_snapshots.json").read_text(encoding="utf-8"))
    monkeypatch.setenv("ALPACA_API_KEY_ID", "fixture-key")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "fixture-secret")
    monkeypatch.setattr("scripts.adapters.alpaca_market_data_adapter.request_json", lambda *args, **kwargs: payload)
    adapter = AlpacaMarketDataAdapter({"enabled": True, "feed": "iex"})
    quote = adapter.fetch_quotes(["AAPL"], liquidity_usd={"AAPL": 100_000_000})["AAPL"]
    assert quote.bid == 210.10
    assert quote.ask == 210.14
    assert quote.last == 210.12
    assert quote.previous_close == 208.40
    assert quote.session_volume == 12_000_000


def test_robinhood_quote_adapter_maps_active_bid_ask_without_broker_actions() -> None:
    adapter = RobinhoodMcpMarketDataAdapter({"enabled": True})
    quotes = adapter._parse_quotes(
        {
            "data": {
                "results": [
                    {
                        "quote": {
                            "symbol": "AAPL",
                            "bid_price": "210.10",
                            "ask_price": "210.14",
                            "last_trade_price": "210.12",
                            "venue_bid_time": "2026-07-21T14:00:00Z",
                            "state": "active",
                            "adjusted_previous_close": "208.40",
                        }
                    }
                ]
            }
        },
        {"AAPL": 100_000_000},
        {"AAPL": "us_equity"},
    )
    assert quotes["AAPL"].bid == 210.10
    assert quotes["AAPL"].ask == 210.14
    assert quotes["AAPL"].source == "robinhood_mcp:get_equity_quotes"


def test_exa_news_filters_future_items_and_keeps_source(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.loads((FIXTURES / "exa_news.json").read_text(encoding="utf-8"))
    payload["results"].append(
        {"title": "Future item", "url": "https://example.com/future", "publishedDate": "2026-07-13T15:00:00Z"}
    )
    monkeypatch.setenv("EXA_API_KEY", "fixture-key")
    monkeypatch.setattr("scripts.adapters.exa_news_adapter.request_json", lambda *args, **kwargs: payload)
    events, sources = ExaNewsAdapter({"enabled": True, "lookback_hours": 48}).search("AAPL", "2026-07-13T14:00:20Z")
    assert len(events) == 1
    assert events[0]["source"] == "sec.gov"
    assert events[0]["source_tier"] == 1
    assert sources[0]["source"] == "sec.gov"


def test_market_clock_uses_nyse_calendar_and_early_close() -> None:
    clock = UsEquityMarketClock()
    regular = clock.status("2026-07-13T15:00:00Z")
    holiday = clock.status("2026-07-04T15:00:00Z")
    early_close = clock.status("2026-11-27T17:55:00Z")
    assert regular.is_regular
    assert holiday.market_session == "closed"
    assert early_close.is_regular
    assert early_close.minutes_to_close == pytest.approx(5.0)


def test_exit_orders_are_not_blocked_by_entry_trade_cap(paper_root: Path) -> None:
    config = load_runtime_config(paper_root)
    config["risk"]["max_daily_trades"] = 0
    broker = PaperBroker(paper_root, config)
    now = "2026-07-13T19:51:00Z"
    quote = Quote("AAPL", 99.9, 100.0, 99.95, "2026-07-13T19:50:55Z", avg_daily_volume_usd=100_000_000)
    positions = {"AAPL": Position("AAPL", 1, 90, "2026-07-10T14:00:00Z", "2026-07-10T14:00:00Z")}
    broker.store.save_positions(positions)
    account = broker.store.account()
    account.cash = 1910
    broker.store.save_account(account, now)
    order = broker.create_order(
        decision_id="exit-cap-test",
        symbol="AAPL",
        side="sell",
        order_type="market",
        quantity=1,
        limit_price=None,
        quote_seen_at=quote.asof,
        idempotency_key="exit-cap-test",
    )
    result = broker.submit_order(order, quote, now)
    assert result.status == "filled"


def test_add_to_existing_position_is_blocked(paper_root: Path) -> None:
    config = load_runtime_config(paper_root)
    broker = PaperBroker(paper_root, config)
    now = "2026-07-13T15:00:00Z"
    quote = Quote("AAPL", 100, 100.05, 100.02, "2026-07-13T14:59:55Z", avg_daily_volume_usd=100_000_000)
    broker.store.save_positions({"AAPL": Position("AAPL", 1, 95, now, now)})
    order = broker.create_order(
        decision_id="add-blocked",
        symbol="AAPL",
        side="buy",
        order_type="limit",
        quantity=1,
        limit_price=quote.ask,
        quote_seen_at=quote.asof,
        idempotency_key="add-blocked",
    )
    assert broker.submit_order(order, quote, now).reject_reason == "adding to an existing position is blocked"


def test_baseline_existing_position_is_not_an_entry_candidate(paper_root: Path) -> None:
    config = load_runtime_config(paper_root)
    snapshot = json.loads((ROOT / "fixtures" / "agent_snapshots" / "snapshots.json").read_text(encoding="utf-8"))["defaults"]
    snapshot["snapshot_id"] = "existing-position-baseline-test"
    snapshot["market_data"]["has_position"] = True
    decision = decide_snapshot(snapshot, config)
    assert decision["action"] == "no_trade"
    assert decision["technical"]["has_position"] is True
    assert "existing position" in decision["technical"]["reasons"][-1]


def test_open_order_expires_without_becoming_position(paper_root: Path) -> None:
    config = load_runtime_config(paper_root)
    config["paper"]["open_order_expiry_seconds"] = 60
    broker = PaperBroker(paper_root, config)
    now = "2026-07-13T15:00:00Z"
    quote = Quote("AAPL", 100.5, 101, 100.75, "2026-07-13T14:59:55Z", avg_daily_volume_usd=100_000_000)
    order = broker.create_order(
        decision_id="expire-open",
        symbol="AAPL",
        side="buy",
        order_type="limit",
        quantity=1,
        limit_price=100,
        quote_seen_at=quote.asof,
        idempotency_key="expire-open",
    )
    opened = broker.submit_order(order, quote, now)
    assert opened.status == "open"
    later = (parse_ts(now) + timedelta(seconds=61)).isoformat()
    processed = broker.process_open_orders({"AAPL": quote}, later)
    assert processed[0].status == "expired"
    assert broker.store.positions() == {}


def test_deterministic_exit_rules() -> None:
    position = Position("AAPL", 1, 100, "2026-07-10T14:00:00Z", "2026-07-10T14:00:00Z")
    quote = Quote("AAPL", 96.9, 97.0, 96.95, "2026-07-13T15:00:00Z")
    decision = evaluate_position_exit(position, quote, "2026-07-13T15:00:01Z", {"stop_loss_pct": 0.03})
    assert decision.should_exit
    assert decision.reason == "deterministic stop loss"


def test_forward_pipeline_dry_run_closes_loop(paper_root: Path) -> None:
    report = run_dry_run(paper_root)
    result = report["result"]
    assert result["event"] == "forward_cycle_complete"
    assert result["selected_candidates"] == ["AAPL"]
    assert result["orders"][0]["order"]["status"] == "filled"
    assert result["shadow_decisions"][0]["action"] == "buy"
    assert result["shadow_decisions"][0]["model_calls"] == 3
    assert report["used_live_order_tools"] is False
    assert "AAPL" in report["paper_positions"]
    assert report["metrics"]["profitability"] == "insufficient_forward_evidence"
    assert report["metrics"]["promotion_eligible"] is False


def test_forward_cycle_refreshes_clock_after_quote_collection(paper_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class LateQuoteAdapter:
        @staticmethod
        def fetch_quotes(symbols, **kwargs):
            quotes = _SyntheticQuotes.fetch_quotes(symbols, **kwargs)
            return {
                symbol: Quote(
                    quote.symbol,
                    quote.bid,
                    quote.ask,
                    quote.last,
                    "2026-07-13T15:00:08Z",
                    source=quote.source,
                    avg_daily_volume_usd=quote.avg_daily_volume_usd,
                    asset_class=quote.asset_class,
                    session_volume=quote.session_volume,
                    previous_close=quote.previous_close,
                )
                for symbol, quote in quotes.items()
            }

    timestamps = iter(["2026-07-13T15:00:00Z", "2026-07-13T15:00:10Z"])
    monkeypatch.setattr("scripts.orchestrator.forward_paper_service.utc_now", lambda: next(timestamps))
    service = ForwardPaperService(paper_root)
    service.vibe = _SyntheticVibe()  # type: ignore[assignment]
    service.quote_adapter = LateQuoteAdapter()  # type: ignore[assignment]
    service.news_adapter = _SyntheticNews()  # type: ignore[assignment]

    result = service.run_once()

    assert result["selected_candidates"] == ["AAPL"]
    assert result["shadow_decisions"][0]["action"] == "buy"
    summary = _cycle_summary(result)
    assert summary["event"] == "forward_cycle_complete"
    assert summary["paper_orders"] == 1


def test_forward_service_handles_keyboard_interrupt_and_releases_lock(paper_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    class FakeService:
        integration_config = {"runtime": {"forward_cycle_seconds": 300}}

        class swarm:
            config = {"enabled": False}

    class FakeScheduler:
        instance = None

        def __init__(self, **kwargs):
            del kwargs
            self.running = False
            self.jobs = []
            FakeScheduler.instance = self

        def add_job(self, callback, *args, **kwargs):
            self.jobs.append((callback, args, kwargs))

        def start(self):
            self.running = True
            raise KeyboardInterrupt

        def shutdown(self, *, wait):
            assert wait is False
            self.running = False

    class FakeLock:
        instance = None

        def __init__(self, path):
            self.path = path
            self.released = False
            FakeLock.instance = self

        def acquire(self):
            return True

        def release(self):
            self.released = True

    monkeypatch.setattr(forward_service_module, "ForwardPaperService", lambda root: FakeService())
    monkeypatch.setattr(forward_service_module, "BlockingScheduler", FakeScheduler)
    monkeypatch.setattr(forward_service_module, "ProcessLock", FakeLock)
    monkeypatch.setattr(forward_service_module.signal, "signal", lambda *_: forward_service_module.signal.SIG_DFL)

    forward_service_module.serve(paper_root)

    events = [json.loads(line)["event"] for line in capsys.readouterr().out.splitlines()]
    assert events == ["forward_service_started", "forward_service_stop_requested", "forward_service_stopped"]
    assert FakeScheduler.instance is not None and not FakeScheduler.instance.running
    assert FakeLock.instance is not None and FakeLock.instance.released


class _ReplayFixtureAdapter:
    def fetch_bars(self, symbols, start_date, end_date, *, interval=None, source=None):
        del start_date, end_date, source
        if interval == "1D":
            start = parse_ts("2026-06-01T00:00:00Z")
            output = {}
            for symbol in symbols:
                slope = 0.5 if symbol == "AAPL" else 0.05
                output[symbol] = [
                    MarketBar(symbol, (start + timedelta(days=index)).isoformat(), 95 + slope * index, 96 + slope * index, 94 + slope * index, 95.5 + slope * index, 1_000_000, "fixture:vibe")
                    for index in range(40)
                ]
            return output
        return {
            symbol: [
                MarketBar(symbol, "2026-07-13T14:00:00Z", 116 if symbol == "AAPL" else 98, 117, 97, 116 if symbol == "AAPL" else 98, 100_000, "fixture:vibe"),
                MarketBar(symbol, "2026-07-13T19:50:00Z", 118 if symbol == "AAPL" else 98.1, 119, 97, 118 if symbol == "AAPL" else 98.1, 1_000_000, "fixture:vibe"),
            ]
            for symbol in symbols
        }

    @staticmethod
    def average_daily_volume_usd(bars, cutoff_time):
        del bars, cutoff_time
        return 100_000_000


def test_vibe_replay_uses_shared_paper_kernel(paper_root: Path) -> None:
    manager = VibeReplayRunManager(
        paper_root,
        "2026-07-13",
        "2026-07-13",
        ["AAPL", "SPY"],
        adapter=_ReplayFixtureAdapter(),
        run_id="fixture-replay",
    )
    result = manager.run()
    assert result["entry_orders"] == 1
    assert result["exit_orders"] == 1
    assert result["metrics"]["closed_trade_count"] == 1
    assert manager.broker.store.positions() == {}
    assert result["limitations"]
