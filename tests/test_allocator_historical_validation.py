from __future__ import annotations

import hashlib
import json
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
                "chain_complete": True,
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


def test_snapshot_verification_rejects_path_escape_and_reference_hash_mismatch(
    tmp_path: Path,
) -> None:
    from scripts.replay.allocator_validation_contracts import (
        verify_immutable_snapshot_file,
    )

    allowed = tmp_path / "snapshots"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    payload = {"decision_time": "2026-07-13T15:00:00+00:00"}
    rendered = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    digest = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
    path = outside / f"snapshot-{digest[:12]}.json"
    path.write_text(
        json.dumps({**payload, "snapshot_hash": digest}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="escapes allowed root"):
        verify_immutable_snapshot_file(path, allowed_root=allowed)
    with pytest.raises(ValueError, match="hash mismatch"):
        verify_immutable_snapshot_file(path, expected_hash="0" * 64)


def test_manifest_hash_covers_source_revision_and_provider(tmp_path: Path) -> None:
    from scripts.replay.allocator_validation_contracts import build_validation_manifest

    project_root = Path(__file__).resolve().parents[1]
    dataset = tmp_path / "snapshot.json"
    dataset.write_text('{"snapshot_id":"fixed"}\n', encoding="utf-8")
    first = build_validation_manifest(
        project_root,
        data_cutoff="2026-07-13T15:00:00+00:00",
        model_id="model-a",
        provider_id="provider-a",
        dataset_paths=[dataset],
        source_revision="revision-a",
    )
    second = build_validation_manifest(
        project_root,
        data_cutoff="2026-07-13T15:00:00+00:00",
        model_id="model-a",
        provider_id="provider-b",
        dataset_paths=[dataset],
        source_revision="revision-b",
    )

    assert first["manifest_hash"] != second["manifest_hash"]
    assert first["provider_id"] == "provider-a"


def test_point_in_time_validation_rejects_cutoff_after_replay_asof() -> None:
    from scripts.replay.allocator_validation_contracts import (
        validate_point_in_time_snapshot,
    )

    result = validate_point_in_time_snapshot(
        {
            "decision_time": "2026-07-13T15:00:01+00:00",
            "data_cutoff_time": "2026-07-13T15:00:01+00:00",
        },
        replay_asof="2026-07-13T15:00:00+00:00",
    )

    assert result["valid"] is False
    assert result["violations"] == [
        {
            "field": "data_cutoff_time",
            "observed_at": "2026-07-13T15:00:01+00:00",
            "reason": "decision_cutoff_after_replay_asof",
        }
    ]


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


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(item, sort_keys=True) + "\n" for item in records),
        encoding="utf-8",
    )


def _historical_snapshot(
    root: Path,
    *,
    ticker: str,
    decision_time: str,
    quote_asof: str | None = None,
    include_quote: bool = True,
) -> dict[str, str]:
    quote = {
        "symbol": ticker,
        "bid": 100.0,
        "ask": 100.02,
        "last": 100.01,
        "asof": quote_asof or decision_time,
    }
    market_context = {
        "ohlcv": {
            "timestamp": decision_time,
            "open": 99.0,
            "high": 100.1,
            "low": 98.9,
            "close": 100.0,
            "volume": 1_000_000,
            "corporate_action_safe": True,
        }
    }
    if include_quote:
        market_context["quote"] = quote
    envelope = {
        "snapshot_type": f"allocator-intraday-{ticker}",
        "decision_time": decision_time,
        "retrieved_at": decision_time,
        "payload": {
            "candidate": {
                "ticker": ticker,
                "market_context": market_context,
            },
            "events": [],
            "source_metadata": [],
            "agent_snapshot": {
                "snapshot_id": f"agent-{ticker}",
                "decision_time": decision_time,
                "data_cutoff_time": decision_time,
                "ticker": ticker,
                "market_data": {"quote": quote} if include_quote else {},
                "technical_signals": {},
                "available_news": [],
                "source_metadata": [],
            },
        },
    }
    serialized = json.dumps(envelope, separators=(",", ":"), sort_keys=True)
    snapshot_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    envelope["snapshot_hash"] = snapshot_hash
    path = (
        root
        / "logs"
        / "ai_instrument_allocator_snapshots"
        / f"{ticker}-{snapshot_hash[:12]}.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(envelope, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"path": str(path), "snapshot_hash": snapshot_hash}


def _all_file_hashes(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(item for item in root.rglob("*") if item.is_file())
    }


def test_natural_strict_replay_reports_funnel_without_creating_historical_orders(
    paper_root: Path,
) -> None:
    from scripts.replay.allocator_historical_replay import (
        run_natural_strict_replay,
    )

    refs = {
        "AAPL": _historical_snapshot(
            paper_root,
            ticker="AAPL",
            decision_time="2026-07-13T15:00:00+00:00",
        ),
        "MSFT": _historical_snapshot(
            paper_root,
            ticker="MSFT",
            decision_time="2026-07-13T15:10:00+00:00",
        ),
        "NVDA": _historical_snapshot(
            paper_root,
            ticker="NVDA",
            decision_time="2026-07-13T15:20:00+00:00",
        ),
    }
    log_dir = (
        paper_root
        / "logs"
        / "strategy_sleeves"
        / "ai_instrument_allocator_v1"
    )
    decisions = [
        {
            "decision_time": "2026-07-13T15:00:00+00:00",
            "ticker": "AAPL",
            "evidence_snapshot": refs["AAPL"],
            "challenge": {"hard_veto_reasons": [], "soft_concerns": []},
            "signal": {"action": "watch", "watch_reason": "Wait for confirmation."},
        },
        {
            "decision_time": "2026-07-13T15:10:00+00:00",
            "ticker": "MSFT",
            "evidence_snapshot": refs["MSFT"],
            "challenge": {"hard_veto_reasons": [], "soft_concerns": []},
            "signal": {"action": "no_trade", "no_trade_reason": "Evidence is ambiguous."},
        },
        {
            "decision_time": "2026-07-13T15:20:00+00:00",
            "ticker": "NVDA",
            "evidence_snapshot": refs["NVDA"],
            "challenge": {"hard_veto_reasons": [], "soft_concerns": []},
            "signal": {"action": "propose_trade"},
        },
    ]
    _write_jsonl(log_dir / "decisions.jsonl", decisions)
    candidate_records = [
        {
            "ticker": ticker,
            "evidence_snapshot": reference,
            "ranking_entered": True,
            "deep_research": True,
            "decision_outcome": (
                "active_plan"
                if ticker == "NVDA"
                else "watch"
                if ticker == "AAPL"
                else "deep_research_no_trade"
            ),
        }
        for ticker, reference in refs.items()
    ]
    _write_jsonl(
        log_dir / "cycles.jsonl",
        [
            {
                "ts": "2026-07-13T15:21:00+00:00",
                "funnel": {"candidate_records": candidate_records},
                "plans": [
                    {
                        "plan_id": "plan-nvda",
                        "ticker": "NVDA",
                        "evidence_snapshot": refs["NVDA"],
                    }
                ],
                "watches": [{"ticker": "AAPL"}],
                "executions": [
                    {
                        "allocation": {"plan_id": "plan-nvda"},
                        "order": {"order_id": "paper-order-nvda", "status": "filled"},
                    }
                ],
                "paper_orders_created": 1,
                "live_order_tools_called": False,
            }
        ],
    )
    _write_jsonl(
        log_dir / "allocations.jsonl",
        [
            {
                "decision_time": "2026-07-13T15:20:00+00:00",
                "data_cutoff_time": "2026-07-13T15:20:00+00:00",
                "plan_id": "plan-nvda",
                "allocation_id": "allocation-nvda",
                "status": "selected",
                "selected_instrument": {
                    "instrument_type": "equity",
                    "ticker": "NVDA",
                },
            }
        ],
    )
    _write_jsonl(
        log_dir / "paper_fills.jsonl",
        [
            {
                "ts": "2026-07-13T15:20:30+00:00",
                "fill": {
                    "order_id": "paper-order-nvda",
                    "symbol": "NVDA",
                    "side": "buy",
                    "filled_at": "2026-07-13T15:20:30+00:00",
                },
            }
        ],
    )
    before = _all_file_hashes(paper_root)

    report = run_natural_strict_replay(
        paper_root,
        hours=48,
        asof="2026-07-13T15:21:00+00:00",
        source_revision="test-revision",
    )

    assert _all_file_hashes(paper_root) == before
    assert report["strict_funnel"]["counts"] == {
        "candidates": 3,
        "ranking_input": 3,
        "deep_research": 3,
        "structured_decisions": 3,
        "watch": 1,
        "no_trade": 1,
        "proposals": 1,
        "allocations": 1,
        "selected_instruments": 1,
        "paper_orders": 1,
        "paper_fills": 1,
    }
    assert report["strict_funnel"]["conversion_rates"]["order_to_fill"] == 1.0
    assert report["rejection_reasons"]["model_no_trade: Evidence is ambiguous."] == 1
    assert report["time_validation"] == {
        "source_violation_count": 0,
        "admitted_violation_count": 0,
        "excluded_snapshot_count": 0,
        "by_field": {},
        "examples": [],
    }
    assert report["issue_3_observed_funnel"]["paper_orders"] == 1
    assert report["historical_orders_created_by_replay"] == 0
    assert report["recorded_historical_orders_observed"] == 1
    assert report["model_calls"] == 0
    assert report["live_broker_write_calls"] == 0
    assert report["live_order_tools_called"] is False
    assert report["llm_replay"] == {
        "mode": "recorded_outputs_only",
        "diagnostic_only": True,
        "current_model_profitability_proof": False,
        "model_calls": 0,
    }
    assert report["manifest"]["source_revision"] == "test-revision"
    assert report["data_completeness"]["equity"]["executable_backtest_ready"] is True
    assert report["data_completeness"]["options"]["executable_pnl_claim_allowed"] is False


def test_natural_strict_replay_excludes_late_and_missing_market_data(
    paper_root: Path,
) -> None:
    from scripts.replay.allocator_historical_replay import (
        run_natural_strict_replay,
    )

    late = _historical_snapshot(
        paper_root,
        ticker="LATE",
        decision_time="2026-07-13T15:00:00+00:00",
        quote_asof="2026-07-13T15:00:01+00:00",
    )
    missing = _historical_snapshot(
        paper_root,
        ticker="MISS",
        decision_time="2026-07-13T15:10:00+00:00",
        include_quote=False,
    )
    log_dir = (
        paper_root
        / "logs"
        / "strategy_sleeves"
        / "ai_instrument_allocator_v1"
    )
    _write_jsonl(
        log_dir / "decisions.jsonl",
        [
            {
                "decision_time": "2026-07-13T15:00:00+00:00",
                "ticker": "LATE",
                "evidence_snapshot": late,
                "signal": {"action": "propose_trade"},
            },
            {
                "decision_time": "2026-07-13T15:10:00+00:00",
                "ticker": "MISS",
                "evidence_snapshot": missing,
                "signal": {"action": "propose_trade"},
            },
        ],
    )
    _write_jsonl(
        log_dir / "cycles.jsonl",
        [
            {
                "ts": "2026-07-13T15:11:00+00:00",
                "funnel": {
                    "candidate_records": [
                        {
                            "ticker": "LATE",
                            "evidence_snapshot": late,
                            "ranking_entered": True,
                            "deep_research": True,
                        },
                        {
                            "ticker": "MISS",
                            "evidence_snapshot": missing,
                            "ranking_entered": True,
                            "deep_research": True,
                        },
                    ]
                },
                "plans": [],
                "executions": [],
                "paper_orders_created": 0,
            }
        ],
    )

    report = run_natural_strict_replay(
        paper_root,
        hours=48,
        asof="2026-07-13T15:11:00+00:00",
    )

    assert report["strict_funnel"]["counts"]["candidates"] == 0
    assert report["strict_funnel"]["counts"]["proposals"] == 0
    assert report["time_validation"]["source_violation_count"] == 1
    assert report["time_validation"]["admitted_violation_count"] == 0
    assert report["time_validation"]["excluded_snapshot_count"] == 2
    assert report["rejection_reasons"][
        "point_in_time: observation_after_decision_cutoff"
    ] == 1
    assert report["rejection_reasons"]["missing_historical_quote"] == 1
    assert report["historical_orders_created_by_replay"] == 0
    assert report["live_broker_write_calls"] == 0
