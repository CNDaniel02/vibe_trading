from __future__ import annotations

import json
from pathlib import Path

import pytest


def _natural_report() -> dict:
    return {
        "evidence_type": "strict_historical_diagnostic",
        "strict_funnel": {
            "counts": {"candidates": 2, "paper_orders": 0},
            "conversion_rates": {},
            "outcome_rates": {},
        },
        "manifest": {
            "strategy_version": "test",
            "prompt_version": "test",
            "schema_version": "allocator-historical-validation-v1",
            "config_hashes": {},
            "model_id": "test",
            "data_cutoff": "2026-07-13T15:00:00+00:00",
            "manifest_hash": "fixed",
        },
        "time_validation": {
            "time_violation_count": 0,
            "source_violation_count": 0,
            "admitted_violation_count": 0,
        },
        "data_completeness": {"equity": {}, "options": {}, "allowed_claims": []},
        "llm_replay": {
            "mode": "recorded_outputs_only",
            "strategy_reexecution_performed": False,
            "diagnostic_only": True,
            "current_model_profitability_proof": False,
        },
        "historical_orders_created_by_replay": 0,
        "live_broker_write_calls": 0,
        "live_order_tools_called": False,
        "historical_performance_available": False,
    }


def _functional_report() -> dict:
    return {
        "evidence_type": "functional_liveness",
        "summary": {
            "scenario_count": 3,
            "passed": 3,
            "failed": 0,
            "live_broker_write_calls": 0,
        },
        "scenarios": [
            {"scenario_id": value, "source_root_unchanged": True}
            for value in ("bullish_equity", "bullish_call", "bearish_put")
        ],
        "historical_performance_claimed": False,
        "forward_performance_claimed": False,
    }


def test_validation_report_keeps_functional_historical_and_forward_evidence_separate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import scripts.replay.allocator_validation_report as report_module

    project_root = Path(__file__).resolve().parents[1]
    monkeypatch.setattr(
        report_module,
        "run_natural_strict_replay",
        lambda *_args, **_kwargs: _natural_report(),
    )
    monkeypatch.setattr(
        report_module,
        "run_golden_path_replay",
        lambda *_args, **_kwargs: _functional_report(),
    )
    monkeypatch.setattr(
        report_module,
        "_forward_evidence",
        lambda *_args, **_kwargs: {
            "evidence_type": "forward_paper_evidence",
            "source": "existing isolated paper sleeve",
            "profitability_claim": "insufficient_forward_evidence",
        },
    )

    result = report_module.build_allocator_validation_report(
        project_root,
        data_root=tmp_path,
        asof="2026-07-13T15:00:00+00:00",
    )

    assert result["functional_liveness"]["status"] == "passed"
    assert result["functional_liveness"]["historical_performance_claimed"] is False
    assert result["historical_performance"]["historical_performance_available"] is False
    assert result["forward_evidence"]["profitability_claim"] == "insufficient_forward_evidence"
    assert result["acceptance"] == {
        "time_violation_count": 0,
        "admitted_time_violation_count": 0,
        "historical_orders_created": 0,
        "live_broker_write_calls": 0,
        "functional_source_root_unchanged": True,
        "forward_state_logs_unchanged": True,
        "forward_protected_file_count": 0,
        "forward_protected_scope": (
            "all files under allocator state, allocator logs, and immutable snapshots"
        ),
    }


def test_validation_report_output_cannot_write_forward_state_or_logs(
    tmp_path: Path,
) -> None:
    from scripts.replay.allocator_validation_report import write_report

    report = {"schema_version": "allocator-historical-validation-v1"}
    with pytest.raises(ValueError, match="state/ or logs"):
        write_report(report, tmp_path / "state" / "report.json", protected_root=tmp_path)
    with pytest.raises(ValueError, match="state/ or logs"):
        write_report(report, tmp_path / "logs" / "report.json", protected_root=tmp_path)

    output = write_report(
        report,
        tmp_path / "reports" / "allocator_validation_latest.json",
        protected_root=tmp_path,
    )
    assert json.loads(output.read_text(encoding="utf-8")) == report


def test_forward_protection_hashes_every_allocator_artifact_type(
    tmp_path: Path,
) -> None:
    from scripts.replay.allocator_validation_report import (
        _protected_forward_hashes,
    )

    state = (
        tmp_path
        / "state"
        / "strategy_sleeves"
        / "ai_instrument_allocator_v1"
    )
    state.mkdir(parents=True)
    (state / "ledger.sqlite").write_bytes(b"fixed-ledger")
    (state / "worker.lock").write_bytes(b"fixed-lock")

    hashes = _protected_forward_hashes(tmp_path)

    assert sorted(hashes) == [
        "state\\strategy_sleeves\\ai_instrument_allocator_v1\\ledger.sqlite",
        "state\\strategy_sleeves\\ai_instrument_allocator_v1\\worker.lock",
    ]


def test_validation_report_can_skip_functional_replay(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import scripts.replay.allocator_validation_report as report_module

    project_root = Path(__file__).resolve().parents[1]
    monkeypatch.setattr(
        report_module,
        "run_natural_strict_replay",
        lambda *_args, **_kwargs: _natural_report(),
    )
    monkeypatch.setattr(
        report_module,
        "run_golden_path_replay",
        lambda *_args, **_kwargs: pytest.fail("functional replay should not run"),
    )
    monkeypatch.setattr(
        report_module,
        "_forward_evidence",
        lambda *_args, **_kwargs: {
            "evidence_type": "forward_paper_evidence",
            "source": "existing isolated paper sleeve",
            "profitability_claim": "insufficient_forward_evidence",
        },
    )

    result = report_module.build_allocator_validation_report(
        project_root,
        data_root=tmp_path,
        asof="2026-07-13T15:00:00+00:00",
        include_functional=False,
    )

    assert result["functional_liveness"]["status"] == "not_run"
    assert result["acceptance"]["functional_source_root_unchanged"] is None
