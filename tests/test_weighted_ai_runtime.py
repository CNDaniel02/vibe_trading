from __future__ import annotations

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from scripts.agents.ai_gated_investment_team import AiGatedInvestmentTeam
from scripts.core.audit import append_jsonl
from scripts.core.config import load_runtime_config
from scripts.core.models import Position, Quote
from scripts.dashboard.paper_dashboard import build_dashboard_state
from scripts.discovery.ai_gated_pipeline import AiGatedPaperPipeline
from scripts.evaluation.outcome_labeler import CandidateOutcomeLabeler
from scripts.evaluation.calculate_metrics import calculate_metrics
from scripts.llm.api_provider import ApiProvider
from scripts.llm.base_provider import ProviderError, ProviderRequest
from scripts.llm.mock_provider import MockProvider
from scripts.llm.usage_tracker import UsageTracker
from scripts.options.models import OptionContract, OptionQuote
from scripts.options.selection import rank_contracts_with_diagnostics
from scripts.options.weighted_strategy import decide_weighted_option_direction
from scripts.orchestrator.forward_paper_service import ForwardPaperService, main as forward_main
from scripts.runtime.heartbeat import write_heartbeat
from scripts.runtime.process_lock import ProcessLock
from scripts.runtime.subprocess_runner import SubprocessJobRunner
from scripts.simulation.paper_broker import PaperBroker
from scripts.strategies.relative_strength_v1 import decide_snapshot as decide_v1
from scripts.strategies.technical_scoring import AdaptiveWeightStore
from scripts.strategies.weighted_relative_strength_v2 import decide_snapshot as decide_weighted


NOW = "2026-07-13T15:00:00+00:00"


def snapshot(*, ticker: str = "AAPL", direction: str = "positive") -> dict:
    return {
        "snapshot_id": f"snapshot-{ticker}",
        "decision_time": NOW,
        "data_cutoff_time": NOW,
        "ticker": ticker,
        "market_session": "regular",
        "market_data": {
            "quote": {
                "symbol": ticker,
                "bid": 100.0,
                "ask": 100.05,
                "last": 100.02,
                "asof": NOW,
                "source": "fixture",
                "avg_daily_volume_usd": 100_000_000,
                "asset_class": "us_equity",
                "is_otc": False,
                "is_leveraged_etf": False,
                "is_inverse_etf": False,
                "halted": False,
                "session_volume": 1_000_000,
                "previous_close": 98.0,
            },
            "market_regime": "neutral",
            "binary_event_within_days": 30,
            "history_fresh": True,
            "has_position": False,
        },
        "technical_signals": {
            "relative_strength_20d": 4.0 if direction == "positive" else -3.0,
            "price_change_1d_pct": 1.0 if direction == "positive" else -2.0,
            "price_change_5d_pct": 6.0 if direction == "positive" else -4.0,
            "volume_ratio": 0.1 if direction == "positive" else 1.0,
            "chase_score": 0.2,
        },
        "available_news": [],
        "source_metadata": [{"source": "fixture", "source_tier": 1, "retrieved_at": NOW}],
    }


def test_weighted_strategy_accepts_strong_total_score_when_one_soft_condition_fails(paper_root: Path) -> None:
    config = load_runtime_config(paper_root)
    value = snapshot()
    assert decide_v1(value, config)["action"] == "no_trade"
    decision = decide_weighted(value, config, paper_root)
    assert decision["feature_scores"]["volume_confirmation"] < 0.1
    assert decision["action"] == "buy"
    assert decision["hard_gate_passed"] is True


def test_weighted_strategies_fail_closed_when_history_freshness_is_missing(paper_root: Path) -> None:
    config = load_runtime_config(paper_root)
    value = snapshot()
    del value["market_data"]["history_fresh"]
    equity = decide_weighted(value, config, paper_root)
    option = decide_weighted_option_direction(value, config)
    assert equity["action"] == "no_trade"
    assert equity["hard_gate_passed"] is False
    assert option["action"] == "no_trade"
    assert "history is stale" in equity["reasons"][0]
    assert "history is stale" in option["reasons"][0]


def test_weighted_equity_blocks_entry_inside_earnings_window(
    paper_root: Path,
) -> None:
    config = load_runtime_config(paper_root)
    value = snapshot()
    value["market_data"]["binary_event_within_days"] = 1
    decision = decide_weighted(value, config, paper_root)
    assert decision["action"] == "no_trade"
    assert decision["hard_gate_passed"] is False
    assert "binary earnings event inside equity exclusion window" in decision["reasons"]


def test_adaptive_min_loss_reduces_weight_of_persistently_wrong_feature(paper_root: Path) -> None:
    profile = {
        "fixed_weights": {
            "relative_strength": 0.30,
            "momentum_1d": 0.15,
            "momentum_5d": 0.20,
            "volume_confirmation": 0.15,
            "market_regime": 0.10,
            "chase_quality": 0.10,
        },
        "adaptive_weights": {
            "enabled": True,
            "minimum_labeled_samples": 1,
            "learning_rate": 2.0,
            "weight_floor": 0.01,
        },
    }
    store = AdaptiveWeightStore(paper_root, profile)
    scores = {
        "relative_strength": 1.0,
        "momentum_1d": 0.0,
        "momentum_5d": 0.0,
        "volume_confirmation": 0.0,
        "market_regime": 0.0,
        "chase_quality": 0.0,
    }
    for _ in range(10):
        state = store.observe(scores, 0.0)
    assert state["mode"] == "adaptive_min_loss"
    assert state["weights"]["relative_strength"] < state["weights"]["momentum_1d"]


def test_company_specific_negative_event_allows_put_in_neutral_market(paper_root: Path) -> None:
    config = load_runtime_config(paper_root)
    value = snapshot(direction="negative")
    value["available_news"] = [
        {
            "headline": "Regulator blocks the company's primary product",
            "published_at": "2026-07-13T14:30:00+00:00",
            "first_seen_at": "2026-07-13T14:31:00+00:00",
            "source": "fda.gov",
            "source_tier": 1,
            "ticker_relevance": 1.0,
            "direction": "negative",
            "confidence": 0.9,
        }
    ]
    decision = decide_weighted_option_direction(value, config)
    assert value["market_data"]["market_regime"] == "neutral"
    assert decision["action"] == "buy_to_open"
    assert decision["option_type"] == "put"
    assert "company-specific" in decision["reasons"][0]


def test_limit_fill_never_crosses_the_agents_limit(paper_root: Path) -> None:
    config = load_runtime_config(paper_root)
    broker = PaperBroker(paper_root, config)
    quote = Quote("AAPL", 100.0, 100.05, 100.02, NOW, avg_daily_volume_usd=100_000_000)
    order = broker.create_order(
        decision_id="limit-boundary",
        symbol="AAPL",
        side="buy",
        order_type="limit",
        quantity=1,
        limit_price=100.05,
        quote_seen_at=NOW,
        now=NOW,
    )
    submitted = broker.submit_order(order, quote, NOW)
    assert submitted.status == "open"
    assert submitted.average_fill_price is None


def test_append_only_log_remains_valid_under_concurrent_writers(paper_root: Path) -> None:
    def write(index: int) -> None:
        append_jsonl(paper_root, "concurrent.jsonl", {"event": "concurrent", "index": index})

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(write, range(200)))
    lines = (paper_root / "logs" / "concurrent.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 200
    assert {json.loads(line)["index"] for line in lines} == set(range(200))


def test_native_timeout_is_retried_and_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    tracker = UsageTracker()
    provider = ApiProvider(
        {
            "model": "fixture-model",
            "base_url": "https://fixture.invalid",
            "api_key_env": "FIXTURE_LLM_KEY",
            "timeout_seconds": 0.01,
            "max_retries": 1,
            "response_format": "json_object",
        },
        tracker,
    )
    monkeypatch.setenv("FIXTURE_LLM_KEY", "secret")
    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("timed out")))
    request = ProviderRequest(
        agent_name="fixture",
        prompt_version="v1",
        system_prompt="Return JSON.",
        input_payload={"snapshot_id": "timeout-snapshot"},
        output_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["ok"],
            "properties": {"ok": {"type": "boolean"}},
        },
        schema_name="fixture",
    )
    with pytest.raises(ProviderError):
        provider.generate(request)
    assert len(tracker.records) == 1
    assert tracker.records[0].retries == 1
    assert "TimeoutError" in str(tracker.records[0].error)


def test_subprocess_runner_kills_worker_at_hard_deadline(
    paper_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProcess:
        pid = 43210
        returncode = -9
        calls = 0

        def communicate(self, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired("fixture", timeout)
            return "", ""

        def poll(self):
            return None

    process = FakeProcess()
    monkeypatch.setattr("scripts.runtime.subprocess_runner.subprocess.Popen", lambda *args, **kwargs: process)
    killed: list[int] = []
    monkeypatch.setattr(
        SubprocessJobRunner,
        "_terminate_process_tree",
        staticmethod(lambda value: killed.append(value.pid)),
    )
    result = SubprocessJobRunner(paper_root).run(
        "fixture",
        ["--once"],
        timeout_seconds=0.01,
        mutates_state=True,
    )
    assert result.status == "timed_out"
    assert result.timed_out is True
    assert killed == [43210]


def test_subprocess_runner_allows_disjoint_resources_and_audits_conflicts(
    paper_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CompletedProcess:
        pid = 43211
        returncode = 0

        @staticmethod
        def communicate(timeout=None):
            del timeout
            return '{"event":"fixture_complete"}', ""

        @staticmethod
        def poll():
            return 0

    runner = SubprocessJobRunner(paper_root)
    runner._active["forward"] = (  # type: ignore[assignment]
        CompletedProcess(),
        frozenset({"main_account"}),
        NOW,
    )
    monkeypatch.setattr(
        "scripts.runtime.subprocess_runner.subprocess.Popen",
        lambda *args, **kwargs: CompletedProcess(),
    )
    disjoint = runner.run(
        "ai_gated",
        ["--ai-gated-once"],
        timeout_seconds=1,
        mutates_state=True,
        resources={"ai_account", "evidence_store"},
    )
    conflict = runner.run(
        "catalyst",
        ["--catalyst-once"],
        timeout_seconds=1,
        mutates_state=True,
        resources={"main_account", "evidence_store"},
    )
    assert disjoint.status == "completed"
    assert conflict.status == "skipped"
    assert "forward" in str(conflict.error)
    runtime_events = [
        json.loads(line)
        for line in (paper_root / "logs" / "runtime_jobs.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert any(item["event"] == "runtime_job_skipped" and item["job_name"] == "catalyst" for item in runtime_events)


def test_research_jobs_are_not_aligned_with_forward_cycle() -> None:
    runtime = load_runtime_config()["integrations"]["runtime"]
    forward_seconds = int(runtime["forward_cycle_seconds"])

    assert int(runtime["catalyst_start_offset_seconds"]) % forward_seconds != 0
    assert int(runtime["ai_gated_start_offset_seconds"]) % forward_seconds != 0


def test_standalone_mutating_cycle_is_blocked_while_service_lock_is_held(
    paper_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = ProcessLock(paper_root / "state" / "forward_service.lock")
    assert lock.acquire()
    monkeypatch.delenv("AUTO_TRADING_SUPERVISED_CHILD", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "forward_paper_service",
            "--root",
            str(paper_root),
            "--once",
            "--now",
            NOW,
        ],
    )
    try:
        with pytest.raises(RuntimeError, match="stop it before running"):
            forward_main()
    finally:
        lock.release()


def test_heartbeat_retries_transient_windows_replace_failure(
    paper_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_replace = Path.replace
    attempts = 0

    def flaky_replace(path: Path, target: Path) -> Path:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError("temporary reader collision")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", flaky_replace)
    record = write_heartbeat(paper_root, payload={"event": "test"}, now=NOW)

    assert attempts == 2
    assert json.loads(
        (paper_root / "state" / "runtime_heartbeat.json").read_text(encoding="utf-8")
    ) == record


def test_dashboard_marks_old_heartbeat_stale(paper_root: Path) -> None:
    (paper_root / "state" / "runtime_heartbeat.json").write_text(
        json.dumps({"last_heartbeat_at": "2026-01-01T00:00:00+00:00", "status": "ok", "payload": {}}),
        encoding="utf-8",
    )
    heartbeat = build_dashboard_state(paper_root)["heartbeat"]
    assert heartbeat["stale"] is True
    assert heartbeat["effective_status"] == "stale"


def test_dashboard_marks_fresh_heartbeat_without_pid_lock_stopped(
    paper_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "scripts.dashboard.paper_dashboard.utc_now",
        lambda: "2026-07-13T15:00:30+00:00",
    )
    (paper_root / "state" / "runtime_heartbeat.json").write_text(
        json.dumps(
            {
                "last_heartbeat_at": "2026-07-13T15:00:00+00:00",
                "status": "ok",
                "payload": {},
            }
        ),
        encoding="utf-8",
    )
    heartbeat = build_dashboard_state(paper_root)["heartbeat"]
    assert heartbeat["stale"] is False
    assert heartbeat["effective_status"] == "stopped"
    assert heartbeat["service_lock"]["status"] == "missing"


def test_metrics_ignore_stale_open_position_snapshot_after_flatten(
    paper_root: Path,
) -> None:
    config = load_runtime_config(paper_root)
    broker = PaperBroker(paper_root, config)
    entry_quote = Quote(
        "AAPL",
        100.0,
        100.05,
        100.02,
        NOW,
        avg_daily_volume_usd=100_000_000,
    )
    entry = broker.create_order(
        decision_id="metrics-entry",
        symbol="AAPL",
        side="buy",
        order_type="market",
        quantity=1,
        limit_price=None,
        quote_seen_at=entry_quote.asof,
        now=NOW,
    )
    broker.submit_order(entry, entry_quote, NOW)
    entry_account = broker.store.account()
    entry_positions = broker.store.positions()
    append_jsonl(
        paper_root,
        "portfolio_snapshots.jsonl",
        {
            "event": "portfolio_snapshot",
            "session": "2026-07-13",
            "asof": NOW,
            "cash": entry_account.cash,
            "equity": entry_account.cash + entry_positions["AAPL"].quantity * entry_quote.bid,
            "equity_unrealized_pnl": -0.06,
            "option_unrealized_pnl": 0,
            "positions": {
                symbol: position.to_dict()
                for symbol, position in entry_positions.items()
            },
            "option_positions": {},
        },
    )
    exit_time = "2026-07-13T15:05:00+00:00"
    exit_quote = Quote(
        "AAPL",
        105.0,
        105.05,
        105.02,
        exit_time,
        avg_daily_volume_usd=100_000_000,
    )
    exit_order = broker.create_order(
        decision_id="metrics-exit",
        symbol="AAPL",
        side="sell",
        order_type="market",
        quantity=1,
        limit_price=None,
        quote_seen_at=exit_quote.asof,
        now=exit_time,
    )
    broker.submit_order(exit_order, exit_quote, exit_time)

    current_account = broker.store.account()
    metrics = calculate_metrics(paper_root)
    assert broker.store.positions() == {}
    assert metrics["valuation_status"] == "cash_flat"
    assert metrics["ending_equity"] == pytest.approx(current_account.cash, abs=0.0001)
    assert metrics["lines"]["equity"]["net_pnl"] == pytest.approx(
        current_account.realized_pnl,
        abs=0.0001,
    )


def test_outcome_labeler_does_not_resolve_before_future_horizon(paper_root: Path) -> None:
    config = load_runtime_config(paper_root)
    decision = decide_weighted(snapshot(), config, paper_root)
    labeler = CandidateOutcomeLabeler(
        paper_root,
        config["strategies"]["weighted_relative_strength_v2"],
        config["costs"],
    )
    labeler.register(decision, snapshot())
    early = Quote("AAPL", 101, 101.05, 101.02, "2026-07-13T15:30:00+00:00")
    assert labeler.resolve({"AAPL": early}, "2026-07-13T15:30:00+00:00") == []
    mature = Quote("AAPL", 101, 101.05, 101.02, "2026-07-13T16:00:00+00:00")
    resolved = labeler.resolve({"AAPL": mature}, "2026-07-13T16:00:00+00:00")
    assert len(resolved) == 1
    assert resolved[0]["exit_quote_asof"] == "2026-07-13T16:00:00+00:00"
    assert resolved[0]["assumed_entry_price"] > 100.05
    assert resolved[0]["assumed_exit_price"] < 101


def test_outcome_labeler_samples_one_overlapping_observation_per_ticker(
    paper_root: Path,
) -> None:
    config = load_runtime_config(paper_root)
    labeler = CandidateOutcomeLabeler(
        paper_root,
        config["strategies"]["weighted_relative_strength_v2"],
        config["costs"],
    )
    first = snapshot()
    first_decision = decide_weighted(first, config, paper_root)
    labeler.register(first_decision, first)
    second = snapshot()
    second["snapshot_id"] = "snapshot-AAPL-five-minutes-later"
    second["decision_time"] = "2026-07-13T15:05:00+00:00"
    second["data_cutoff_time"] = second["decision_time"]
    second["market_data"]["quote"]["asof"] = second["decision_time"]
    second_decision = decide_weighted(second, config, paper_root)
    labeler.register(second_decision, second)
    pending = json.loads(
        (paper_root / "state" / "pending_candidate_outcomes.json").read_text(
            encoding="utf-8"
        )
    )
    assert list(pending) == [first["snapshot_id"]]


def test_outcome_labeler_does_not_schedule_target_after_market_close(
    paper_root: Path,
) -> None:
    config = load_runtime_config(paper_root)
    labeler = CandidateOutcomeLabeler(
        paper_root,
        config["strategies"]["weighted_relative_strength_v2"],
        config["costs"],
    )
    late = snapshot()
    late["snapshot_id"] = "late-observation"
    late["decision_time"] = "2026-07-13T19:30:00+00:00"
    late["data_cutoff_time"] = late["decision_time"]
    late["market_data"]["quote"]["asof"] = late["decision_time"]
    labeler.register(decide_weighted(late, config, paper_root), late)
    assert not (
        paper_root / "state" / "pending_candidate_outcomes.json"
    ).exists()


def test_weighted_options_do_not_treat_missing_event_as_zero_score(
    paper_root: Path,
) -> None:
    config = load_runtime_config(paper_root)
    decision = decide_weighted_option_direction(snapshot(), config)
    assert decision["action"] == "buy_to_open"
    assert decision["option_type"] == "call"
    assert decision["call_score"] >= 0.54


def test_option_selection_returns_exact_rejection_diagnostics(paper_root: Path) -> None:
    config = load_runtime_config(paper_root)
    contract = OptionContract("opt", "chain", "AAPL", "put", 100, "2026-08-07")
    quote = OptionQuote(
        "opt",
        0.50,
        1.00,
        0.75,
        NOW,
        "fixture",
        delta=-0.45,
        gamma=0.04,
        theta=-0.03,
        vega=0.08,
        implied_volatility=0.3,
        volume=1000,
        open_interest=1000,
    )
    ranked, diagnostics = rank_contracts_with_diagnostics([contract], {"opt": quote}, NOW, config)
    assert ranked == []
    assert diagnostics["rejections"]["option spread too wide"] == 1


class _Discovery:
    def collect_seed_candidates(self, decision_time, core_watchlist):
        del decision_time, core_watchlist
        return [{"ticker": "AAPL", "sources": ["fixture"]}]

    def fetch_market_context(self, symbols, decision_time):
        del symbols
        return {
            "AAPL": {
                "ticker": "AAPL",
                "eligible": True,
                "quote": {
                    "symbol": "AAPL",
                    "bid": 100.0,
                    "ask": 100.05,
                    "last": 100.02,
                    "asof": decision_time,
                    "source": "fixture",
                    "avg_daily_volume_usd": 100_000_000,
                    "asset_class": "us_equity",
                    "is_otc": False,
                    "is_leveraged_etf": False,
                    "is_inverse_etf": False,
                    "halted": False,
                    "session_volume": 1_000_000,
                    "previous_close": 98.0,
                },
                "fundamentals": {"market_cap": 3_000_000_000_000},
                "technical_signals": {
                    "price_change_1d_pct": 2.0,
                    "price_change_5d_pct": 6.0,
                    "relative_strength_20d": 5.0,
                    "volume_ratio": 1.5,
                },
            }
        }

    @staticmethod
    def validate_instrument(symbol):
        return {"valid": True, "name": "Apple Inc.", "symbol": symbol}

    @staticmethod
    def fetch_current_quote(symbol, **kwargs):
        del kwargs
        return Quote(symbol, 100.0, 100.05, 100.02, NOW, avg_daily_volume_usd=100_000_000)


class _News:
    @staticmethod
    def search(ticker, decision_time, company_name=None):
        del company_name
        event = {
            "ticker": ticker,
            "headline": "Company raises guidance",
            "published_at": "2026-07-13T14:30:00+00:00",
            "event_at": "2026-07-13T14:25:00+00:00",
            "first_seen_at": "2026-07-13T14:31:00+00:00",
            "retrieved_at": decision_time,
            "source": "company.example",
            "source_tier": 1,
            "ticker_relevance": 1.0,
            "direction": "positive",
            "novelty": 0.9,
            "already_priced_in": False,
            "confidence": 0.9,
            "url": "https://company.example/investors/guidance",
            "highlights": ["Full-year guidance increased."],
        }
        return [event], [{"source": "company.example", "source_tier": 1, "retrieved_at": decision_time}]


class _OptionData:
    @staticmethod
    def fetch_quotes(option_ids):
        assert option_ids == []
        return {}


def test_ai_candidate_selection_reserves_reported_earnings_and_both_directions(
    paper_root: Path,
) -> None:
    config = load_runtime_config(paper_root)
    tracker = UsageTracker()
    pipeline = AiGatedPaperPipeline(
        paper_root,
        config,
        MockProvider(tracker),
        tracker,
        discovery_adapter=_Discovery(),
        news_adapter=_News(),
        option_data=_OptionData(),
    )
    candidates = [
        {
            "ticker": f"BEAR{index}",
            "pre_score": 0.90 - index / 100,
            "technical_direction": "bearish",
            "reported_earnings": None,
        }
        for index in range(6)
    ]
    candidates.extend(
        [
            {
                "ticker": "BULL",
                "pre_score": 0.60,
                "technical_direction": "bullish",
                "reported_earnings": None,
            },
            {
                "ticker": "MSFT",
                "pre_score": 0.55,
                "technical_direction": "bullish",
                "reported_earnings": {
                    "eps_surprise_ratio": 0.12,
                    "eps_actual": 4.74,
                    "eps_estimate": 4.23,
                },
            },
        ]
    )
    selected = pipeline._select_technical_candidates(candidates, 6)
    assert "MSFT" in {item["ticker"] for item in selected}
    assert sum(
        item["technical_direction"] == "bullish"
        for item in selected
    ) >= 2
    assert sum(
        item["technical_direction"] == "bearish"
        for item in selected
    ) >= 2


def test_reported_earnings_priority_requires_event_to_be_available() -> None:
    seed = {
        "source_details": [
            {
                "source": "earnings_calendar",
                "detail": {
                    "eps": {"actual": "4.74", "estimate": "4.23"},
                    "report": {
                        "date": "2026-07-29",
                        "timing": "pm",
                        "verified": True,
                    },
                    "eps_surprise_ratio": 0.120567,
                },
            }
        ]
    }
    available = AiGatedPaperPipeline._confirmed_reported_earnings(
        seed,
        "2026-07-30",
    )
    assert available is not None
    assert available["eps_actual"] == 4.74
    assert (
        AiGatedPaperPipeline._confirmed_reported_earnings(
            seed,
            "2026-07-29",
        )
        is None
    )


class _PremarketNews(_News):
    @staticmethod
    def search(ticker, decision_time, company_name=None):
        events, sources = _News.search(
            ticker,
            decision_time,
            company_name,
        )
        events[0]["published_at"] = "2026-07-13T12:30:00+00:00"
        events[0]["event_at"] = "2026-07-13T12:25:00+00:00"
        events[0]["first_seen_at"] = "2026-07-13T12:31:00+00:00"
        return events, sources


def test_ai_premarket_research_never_creates_order(
    paper_root: Path,
) -> None:
    config = load_runtime_config(paper_root)
    tracker = UsageTracker()
    pipeline = AiGatedPaperPipeline(
        paper_root,
        config,
        MockProvider(tracker),
        tracker,
        discovery_adapter=_Discovery(),
        news_adapter=_PremarketNews(),
        option_data=_OptionData(),
    )
    result = pipeline.run("2026-07-13T13:00:00+00:00")
    assert result["event"] == "ai_gated_cycle_complete"
    assert result["research_only"] is True
    assert result["paper_orders_created"] == 0
    assert result["live_order_tools_called"] is False
    assert PaperBroker(
        paper_root,
        config,
        namespace="ai_gated_technical_v1",
    ).store.positions() == {}
    assert all(
        item["execution"]["status"] in {"research_only", "no_trade"}
        for item in result["decisions"]
    )


def test_ai_gated_pipeline_executes_only_in_isolated_paper_sleeve(paper_root: Path) -> None:
    config = load_runtime_config(paper_root)
    tracker = UsageTracker()
    pipeline = AiGatedPaperPipeline(
        paper_root,
        config,
        MockProvider(tracker),
        tracker,
        discovery_adapter=_Discovery(),
        news_adapter=_News(),
        option_data=_OptionData(),
    )
    result = pipeline.run(NOW)
    assert result["event"] == "ai_gated_cycle_complete"
    assert result["model_calls"] == 4
    assert result["paper_orders_created"] == 1
    assert result["live_order_tools_called"] is False
    assert PaperBroker(paper_root, config).store.positions() == {}
    sleeve = PaperBroker(paper_root, config, namespace="ai_gated_technical_v1")
    assert "AAPL" in sleeve.store.positions()
    assert sleeve.store.account().cash < 2000
    assert {"place_order", "review_order", "replace_order"}.isdisjoint(dir(sleeve))


def test_ai_gated_pipeline_skips_model_research_near_close(
    paper_root: Path,
) -> None:
    config = load_runtime_config(paper_root)
    tracker = UsageTracker()
    pipeline = AiGatedPaperPipeline(
        paper_root,
        config,
        MockProvider(tracker),
        tracker,
        discovery_adapter=_Discovery(),
        news_adapter=_News(),
        option_data=_OptionData(),
    )
    result = pipeline.run("2026-07-13T19:40:00+00:00")
    assert result["event"] == "ai_gated_cycle_skipped"
    assert "insufficient time before market close" in result["reason"]
    assert result["model_calls"] == 0
    assert tracker.records == []


def test_eod_guard_recovers_overnight_equity_position(
    paper_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ForwardPaperService(paper_root)
    service.broker.store.save_positions(
        {"AAPL": Position("AAPL", 1, 100, "2026-07-10T15:00:00+00:00", "2026-07-10T15:00:00+00:00")}
    )
    account = service.broker.store.account()
    account.cash = 1900
    service.broker.store.save_account(account, "2026-07-10T15:00:00+00:00")
    quote = Quote("AAPL", 101.0, 101.05, 101.02, NOW, avg_daily_volume_usd=100_000_000)
    monkeypatch.setattr(service, "_fetch_eod_equity_quotes", lambda positions: {"AAPL": quote})
    monkeypatch.setattr(
        service.ai_gated_pipeline,
        "monitor_only",
        lambda now, force_flatten=False: {"event": "fixture", "force_flatten": force_flatten},
    )
    result = service.run_eod_guard(NOW)
    assert result["reason"] == "overnight recovery flatten"
    assert result["equity_exits"][0]["status"] == "filled"
    assert service.broker.store.positions() == {}
