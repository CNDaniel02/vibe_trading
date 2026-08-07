from __future__ import annotations

import os
from pathlib import Path

from scripts.agents.investment_team import run_investment_team
from scripts.core.config import load_runtime_config
from scripts.core.models import Quote
from scripts.replay.historical_data_adapter import CsvHistoricalMarketDataAdapter
from scripts.replay.replay_run_manager import ReplayRunManager
from scripts.replay.virtual_clock import VirtualClock
from scripts.runtime.healthcheck import run_healthcheck
from scripts.runtime.heartbeat import write_heartbeat
from scripts.runtime.process_lock import ProcessLock
from scripts.runtime.scheduler import PaperScheduler
from scripts.runtime.watchdog import check_runtime


def quote() -> Quote:
    return Quote(
        symbol="SPY",
        bid=100.0,
        ask=100.05,
        last=100.03,
        asof="2026-07-04T14:00:00+00:00",
        source="test",
        avg_daily_volume_usd=500_000_000,
        asset_class="us_etf",
    )


def test_investment_team_structures_challenge_gate_and_gaps():
    decision = run_investment_team("SPY", quote(), {"now": "2026-07-04T14:00:30+00:00"})
    assert decision.symbol == "SPY"
    assert len(decision.tasks) == 4
    assert decision.reviewer_status in {"approved", "challenged", "rejected"}
    assert decision.source_gaps
    assert decision.assumptions
    assert decision.red_lines
    assert decision.invalidation_triggers
    assert any(report.bull_case and report.bear_case for report in decision.reports)


def test_virtual_clock_rejects_backwards_time():
    clock = VirtualClock()
    clock.advance_to("2026-07-04T14:00:00+00:00")
    try:
        clock.advance_to("2026-07-04T13:59:59+00:00")
        raised = False
    except ValueError:
        raised = True
    assert raised


def test_historical_adapter_loads_replay_quotes():
    path = Path(__file__).parent / "fixtures" / "historical_quotes.csv"
    events = CsvHistoricalMarketDataAdapter(path).events()
    assert len(events) == 2
    assert events[0].quote.bid == 100.0
    assert events[0].quote.ask == 100.05


def test_replay_uses_paper_broker_and_state(paper_root):
    path = Path(__file__).parent / "fixtures" / "historical_quotes.csv"
    result = ReplayRunManager(paper_root, path).run(max_events=1)
    assert result["event"] == "historical_replay_complete"
    orders = (paper_root / "state" / "paper_orders.json").read_text(encoding="utf-8")
    assert "team_SPY" in orders
    assert "paper_fills.jsonl" in {p.name for p in (paper_root / "logs").iterdir()}


def test_heartbeat_watchdog_fresh_and_stale(paper_root):
    lock = ProcessLock(paper_root / "state" / "forward_service.lock")
    assert lock.acquire()
    try:
        write_heartbeat(paper_root, now="2026-07-04T14:00:00+00:00")
        fresh = check_runtime(paper_root, max_heartbeat_age_seconds=120, now="2026-07-04T14:01:00+00:00")
        stale = check_runtime(paper_root, max_heartbeat_age_seconds=30, now="2026-07-04T14:01:00+00:00")
        assert fresh.healthy and not fresh.fail_closed
        assert not stale.healthy and stale.fail_closed
        assert stale.reason == "stale heartbeat"
    finally:
        lock.release()


def test_watchdog_rejects_fresh_heartbeat_without_running_service(paper_root):
    write_heartbeat(paper_root, now="2026-07-04T14:00:00+00:00")
    decision = check_runtime(
        paper_root,
        max_heartbeat_age_seconds=120,
        now="2026-07-04T14:01:00+00:00",
    )
    assert not decision.healthy
    assert decision.fail_closed
    assert decision.reason == "forward service lock missing"


def test_process_lock_blocks_second_acquire(paper_root):
    lock_path = paper_root / "state" / "runner.lock"
    first = ProcessLock(lock_path)
    second = ProcessLock(lock_path)
    try:
        assert first.acquire()
        assert not second.acquire()
    finally:
        first.release()


def test_process_lock_recovers_confirmed_stale_owner(paper_root):
    lock_path = paper_root / "state" / "runner.lock"
    lock_path.write_text("999999999", encoding="ascii")
    lock = ProcessLock(lock_path)
    try:
        assert lock.acquire()
        assert lock_path.read_text(encoding="ascii") == str(os.getpid())
    finally:
        lock.release()


def test_healthcheck_and_scheduler_wrapper(paper_root):
    health = run_healthcheck(paper_root)
    assert health["ok"] == health["full_forward_evaluation_ready"]
    assert health["quote_provider"] in {"alpaca", "robinhood_mcp"}
    assert health["forward_ready"] == bool(
        health["integrations"]["vibe"]["ready"]
        and (
            health["quote_data"]["ready"]
            or (
                health["fallback_quote_data"]
                and health["fallback_quote_data"]["ready"]
            )
        )
    )
    assert health["runtime_healthy"] is True
    assert health["operational_status"] == (
        "ok" if health["full_forward_evaluation_ready"] else "degraded"
    )
    if health["operational_status"] == "degraded":
        assert health["degraded_reasons"]
    scheduler = PaperScheduler(paper_root)
    config = load_runtime_config(paper_root)
    scheduler.add_interval_job("noop", 60, lambda: config["paper"]["mode"]["paper"])
    assert scheduler.scheduler.get_job("noop") is not None
    scheduler.shutdown()
