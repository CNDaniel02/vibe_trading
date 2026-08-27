from __future__ import annotations

import hashlib
from pathlib import Path

import pytest


def _complete_equity_row() -> dict:
    return {
        "ticker": "AAPL",
        "asof": "2026-07-13T15:00:00+00:00",
        "bid": 100.0,
        "ask": 100.02,
        "ohlcv": {
            "timestamp": "2026-07-13T14:59:00+00:00",
            "open": 99.0,
            "high": 100.1,
            "low": 98.9,
            "close": 100.0,
            "volume": 1_000_000,
            "corporate_action_safe": True,
        },
    }


def _complete_option_contract() -> dict:
    return {
        "option_id": "aapl-20260821-100-c",
        "chain_id": "aapl-20260821",
        "underlying": "AAPL",
        "option_type": "call",
        "strike_price": 100.0,
        "expiration_date": "2026-08-21",
        "bid": 2.0,
        "ask": 2.02,
        "implied_volatility": 0.25,
        "delta": 0.52,
        "gamma": 0.04,
        "theta": -0.03,
        "vega": 0.08,
        "volume": 2000,
        "open_interest": 10_000,
        "updated_at": "2026-07-13T15:00:00+00:00",
    }


def test_point_in_time_validation_rejects_late_news_quote_and_option_data() -> None:
    from scripts.replay.allocator_validation_contracts import (
        validate_point_in_time_snapshot,
    )

    snapshot = {
        "snapshot_id": "late-observations",
        "decision_time": "2026-07-13T15:00:00+00:00",
        "data_cutoff_time": "2026-07-13T15:00:00+00:00",
        "market_data": {
            "quote": {"asof": "2026-07-13T15:00:01+00:00"},
            "option_chain": [
                {"option_id": "late-call", "updated_at": "2026-07-13T15:00:03+00:00"}
            ],
        },
        "available_news": [
            {
                "published_at": "2026-07-13T14:00:00+00:00",
                "first_seen_at": "2026-07-13T15:00:02+00:00",
                "retrieved_at": "2026-07-13T15:00:04+00:00",
            }
        ],
    }

    result = validate_point_in_time_snapshot(snapshot)

    assert result["valid"] is False
    assert result["cutoff"] == "2026-07-13T15:00:00+00:00"
    assert {item["field"] for item in result["violations"]} == {
        "market_data.quote.asof",
        "market_data.option_chain[0].updated_at",
        "available_news[0].first_seen_at",
        "available_news[0].retrieved_at",
    }


def test_point_in_time_validation_accepts_observations_at_or_before_cutoff() -> None:
    from scripts.replay.allocator_validation_contracts import (
        validate_point_in_time_snapshot,
    )

    snapshot = {
        "snapshot_id": "valid",
        "decision_time": "2026-07-13T15:00:00+00:00",
        "data_cutoff_time": "2026-07-13T15:00:00+00:00",
        "market_data": {"quote": {"asof": "2026-07-13T15:00:00+00:00"}},
        "available_news": [
            {
                "published_at": "2026-07-13T14:00:00+00:00",
                "first_seen_at": "2026-07-13T14:30:00+00:00",
                "retrieved_at": "2026-07-13T14:31:00+00:00",
            }
        ],
    }

    result = validate_point_in_time_snapshot(snapshot)

    assert result == {
        "valid": True,
        "cutoff": "2026-07-13T15:00:00+00:00",
        "violation_count": 0,
        "violations": [],
    }


def test_validation_manifest_freezes_strategy_prompts_schemas_configs_and_data(
    tmp_path: Path,
) -> None:
    from scripts.replay.allocator_validation_contracts import (
        VALIDATION_SCHEMA_VERSION,
        build_validation_manifest,
    )

    project_root = Path(__file__).resolve().parents[1]
    dataset = tmp_path / "snapshot.json"
    dataset.write_text('{"snapshot_id":"fixed"}\n', encoding="utf-8")

    manifest = build_validation_manifest(
        project_root,
        data_cutoff="2026-07-13T15:00:00+00:00",
        model_id="deterministic-fixture-v1",
        dataset_paths=[dataset],
        source_revision="594a273-test",
    )

    assert manifest["strategy"] == "ai_instrument_allocator_v1"
    assert manifest["strategy_version"].startswith("ai_instrument_allocator_v1@")
    assert manifest["source_revision"] == "594a273-test"
    assert manifest["prompt_version"] == "v3-allocator-recall"
    assert manifest["schema_version"] == VALIDATION_SCHEMA_VERSION
    assert manifest["model_id"] == "deterministic-fixture-v1"
    assert manifest["data_cutoff"] == "2026-07-13T15:00:00+00:00"
    assert set(manifest["prompt_hashes"]) >= {
        "ai_allocator_ranker.md",
        "ai_allocator_news_agent.md",
        "ai_allocator_challenge_agent.md",
        "ai_allocator_decision_manager.md",
    }
    assert set(manifest["schema_hashes"]) >= {
        "ai_allocator_signal.schema.json",
        "ai_allocator_challenge.schema.json",
    }
    assert set(manifest["config_hashes"]) >= {
        "strategy_profiles.yaml",
        "paper_risk_limits.yaml",
        "options_risk_limits.yaml",
        "shared_risk_limits.yaml",
    }
    expected = hashlib.sha256(dataset.read_bytes()).hexdigest()
    assert manifest["dataset_hashes"][str(dataset.resolve())] == expected


def test_market_data_completeness_blocks_executable_option_pnl_when_chain_is_incomplete() -> None:
    from scripts.replay.allocator_validation_contracts import (
        assess_market_data_completeness,
    )

    option = _complete_option_contract()
    option.pop("vega")
    result = assess_market_data_completeness(
        equity_rows=[_complete_equity_row()],
        option_chain_snapshots=[
            {
                "underlying": "AAPL",
                "asof": "2026-07-13T15:00:00+00:00",
                "contracts": [option],
            }
        ],
    )

    assert result["equity"]["executable_backtest_ready"] is True
    assert result["options"]["executable_backtest_ready"] is False
    assert result["options"]["executable_pnl_claim_allowed"] is False
    assert result["options"]["synthetic_sensitivity_allowed"] is True
    assert result["options"]["missing_fields"] == {"contracts[].vega": 1}
    assert result["allowed_claims"] == [
        "equity_backtest",
        "synthetic_option_sensitivity",
    ]


def test_market_data_completeness_requires_corporate_action_safe_ohlcv() -> None:
    from scripts.replay.allocator_validation_contracts import (
        assess_market_data_completeness,
    )

    equity = _complete_equity_row()
    equity["ohlcv"]["corporate_action_safe"] = False

    result = assess_market_data_completeness(
        equity_rows=[equity],
        option_chain_snapshots=[],
    )

    assert result["equity"]["executable_backtest_ready"] is False
    assert result["equity"]["missing_fields"] == {
        "ohlcv.corporate_action_safe=true": 1
    }
    assert "executable_option_pnl" not in result["allowed_claims"]


@pytest.mark.parametrize("mode", ["expanding", "rolling"])
def test_walk_forward_partitions_are_horizon_separated_maturity_safe_and_disjoint(
    mode: str,
) -> None:
    from scripts.replay.allocator_validation_contracts import (
        build_walk_forward_partitions,
    )

    records = [
        {
            "record_id": "dev-1",
            "horizon": "next_close",
            "decision_time": "2026-07-01T15:00:00+00:00",
            "label_matured_at": "2026-07-02T20:00:00+00:00",
        },
        {
            "record_id": "other-horizon",
            "horizon": "intraday_close",
            "decision_time": "2026-07-02T15:00:00+00:00",
            "label_matured_at": "2026-07-02T20:00:00+00:00",
        },
        {
            "record_id": "dev-late-label",
            "horizon": "next_close",
            "decision_time": "2026-07-03T15:00:00+00:00",
            "label_matured_at": "2026-07-08T20:00:00+00:00",
        },
        {
            "record_id": "cal-1",
            "horizon": "next_close",
            "decision_time": "2026-07-07T15:00:00+00:00",
            "label_matured_at": "2026-07-08T20:00:00+00:00",
        },
        {
            "record_id": "holdout-1",
            "horizon": "next_close",
            "decision_time": "2026-07-10T15:00:00+00:00",
            "label_matured_at": "2026-07-13T20:00:00+00:00",
        },
    ]

    result = build_walk_forward_partitions(
        records,
        horizon="next_close",
        development_end="2026-07-06T23:59:59+00:00",
        calibration_end="2026-07-09T23:59:59+00:00",
        holdout_end="2026-07-14T23:59:59+00:00",
        mode=mode,
        rolling_train_size=1 if mode == "rolling" else None,
    )

    assert [item["record_id"] for item in result["partitions"]["development"]] == [
        "dev-1",
        "dev-late-label",
    ]
    assert [item["record_id"] for item in result["partitions"]["calibration"]] == [
        "cal-1"
    ]
    assert [item["record_id"] for item in result["partitions"]["final_holdout"]] == [
        "holdout-1"
    ]
    calibration_fold = result["calibration_folds"][0]
    assert [item["record_id"] for item in calibration_fold["training_records"]] == [
        "dev-1"
    ]
    holdout_fold = result["holdout_folds"][0]
    assert "holdout-1" not in {
        item["record_id"] for item in holdout_fold["training_records"]
    }
    assert all(
        item["label_matured_at"] <= holdout_fold["training_cutoff_time"]
        for item in holdout_fold["training_records"]
    )
    assert result["leakage_checks"] == {
        "partition_overlap_count": 0,
        "future_or_unmatured_training_count": 0,
        "holdout_used_for_training_count": 0,
    }


def test_funnel_reports_stage_conversions_and_branch_rates() -> None:
    from scripts.replay.allocator_validation_contracts import funnel_with_conversion

    result = funnel_with_conversion(
        {
            "candidates": 10,
            "structured_decisions": 8,
            "watch": 2,
            "no_trade": 3,
            "proposals": 3,
            "allocations": 2,
            "paper_orders": 1,
            "paper_fills": 1,
        }
    )

    assert result["counts"]["proposals"] == 3
    assert result["conversion_rates"]["candidate_to_structured_decision"] == 0.8
    assert result["conversion_rates"]["proposal_to_allocation"] == pytest.approx(
        2 / 3
    )
    assert result["outcome_rates"]["watch"] == 0.25
    assert result["outcome_rates"]["no_trade"] == 0.375
    assert result["outcome_rates"]["proposal"] == 0.375
