from __future__ import annotations

import hashlib
from pathlib import Path

import pytest


def _protected_source_hashes(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for directory in (
        root / "state" / "strategy_sleeves" / "ai_instrument_allocator_v1",
        root / "logs" / "strategy_sleeves" / "ai_instrument_allocator_v1",
    ):
        if not directory.exists():
            continue
        for path in sorted(item for item in directory.rglob("*") if item.is_file()):
            result[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


@pytest.mark.parametrize(
    ("scenario_id", "expected_instrument"),
    [
        ("bullish_equity", "equity"),
        ("bullish_call", "call"),
        ("bearish_put", "put"),
    ],
)
def test_golden_path_uses_formal_allocator_broker_wal_mandate_exit_and_pnl(
    scenario_id: str,
    expected_instrument: str,
) -> None:
    from scripts.replay.allocator_functional_replay import (
        run_golden_path_replay,
    )

    project_root = Path(__file__).resolve().parents[1]
    report = run_golden_path_replay(project_root, scenario_ids=[scenario_id])

    scenario = report["scenarios"][0]
    assert scenario["scenario_id"] == scenario_id
    assert scenario["status"] == "passed"
    assert scenario["selected_instrument"] == expected_instrument
    assert scenario["trace"] == {
        "proposal": True,
        "plan_persisted": True,
        "preopen_revalidated": True,
        "allocation_selected": True,
        "deterministic_risk_approved": True,
        "paper_order_created": True,
        "entry_filled": True,
        "fill_wal_committed": True,
        "mandate_open": True,
        "exit_evaluated": True,
        "exit_filled": True,
        "mandate_closed": True,
        "pnl_attributed": True,
        "paper_broker_boundary": True,
        "live_write_guard_clean": True,
    }
    assert scenario["entry_order_status"] == "filled"
    assert scenario["exit_order_status"] == "filled"
    assert scenario["wal_committed_transactions"] == 2
    assert scenario["wal_identity_valid"] is True
    assert scenario["wal_order_ids"] == scenario["expected_wal_order_ids"]
    assert all(scenario["deterministic_risk_evidence"].values())
    assert scenario["closed_trade_count"] == 1
    assert scenario["realized_pnl_usd"] > 0
    assert scenario["manifest"]["source_revision"] != "not_recorded"
    assert scenario["live_broker_write_calls"] == 0
    assert scenario["live_write_guard"]["installed"] is True
    assert scenario["live_write_guard"]["attempts"] == []
    assert scenario["live_write_guard"]["paper_broker_boundary_verified"] is True
    assert scenario["live_order_tools_called"] is False
    assert scenario["write_scope"] == "temporary_root_only"
    assert scenario["temporary_root_exists_after"] is False
    assert report["evidence_type"] == "functional_liveness"
    assert report["historical_performance_claimed"] is False
    assert report["forward_performance_claimed"] is False


def test_golden_path_does_not_modify_forward_allocator_state_or_logs() -> None:
    from scripts.replay.allocator_functional_replay import (
        run_golden_path_replay,
    )

    project_root = Path(__file__).resolve().parents[1]
    before = _protected_source_hashes(project_root)

    report = run_golden_path_replay(project_root)

    assert _protected_source_hashes(project_root) == before
    assert report["summary"] == {
        "scenario_count": 3,
        "passed": 3,
        "failed": 0,
        "live_broker_write_calls": 0,
    }
    assert all(item["source_root_unchanged"] for item in report["scenarios"])


def test_golden_path_never_constructs_network_market_data_adapters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.discovery.ai_gated_pipeline as pipeline_module
    from scripts.replay.allocator_functional_replay import run_golden_path_replay

    def blocked(*_args, **_kwargs):
        pytest.fail("golden replay attempted to construct a network adapter")

    monkeypatch.setattr(pipeline_module, "RobinhoodDiscoveryAdapter", blocked)
    monkeypatch.setattr(pipeline_module, "ExaNewsAdapter", blocked)
    monkeypatch.setattr(pipeline_module, "RobinhoodOptionMarketDataAdapter", blocked)

    report = run_golden_path_replay(
        Path(__file__).resolve().parents[1],
        scenario_ids=["bearish_put"],
    )

    assert report["summary"]["passed"] == 1
    assert report["summary"]["live_broker_write_calls"] == 0


def test_golden_live_write_guard_counts_and_blocks_adapter_attempt() -> None:
    from scripts.broker.robinhood_readonly_adapter import (
        LiveOrderToolBlocked,
        RobinhoodReadonlyAdapter,
    )
    from scripts.replay.allocator_functional_replay import (
        _deny_live_broker_writes,
    )

    attempts: list[dict[str, str]] = []
    with pytest.raises(LiveOrderToolBlocked):
        with _deny_live_broker_writes() as attempts:
            RobinhoodReadonlyAdapter().place_equity_order(symbol="SPY")

    assert attempts == [
        {"boundary": "readonly_adapter", "tool": "place_equity_order"}
    ]
