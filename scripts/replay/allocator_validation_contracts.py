from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

import yaml

from scripts.core.models import parse_ts


VALIDATION_SCHEMA_VERSION = "allocator-historical-validation-v1"
_STRATEGY = "ai_instrument_allocator_v1"
_OBSERVATION_TIME_FIELDS = {
    "asof",
    "event_at",
    "first_seen_at",
    "nav_calculated_at",
    "published_at",
    "quote_seen_at",
    "retrieved_at",
    "updated_at",
}
_PROMPT_FILES = (
    "ai_allocator_ranker.md",
    "ai_allocator_news_agent.md",
    "ai_allocator_challenge_agent.md",
    "ai_allocator_decision_manager.md",
)
_SCHEMA_FILES = (
    "ai_allocator_signal.schema.json",
    "ai_allocator_challenge.schema.json",
)
_CONFIG_FILES = (
    "strategy_profiles.yaml",
    "paper_mode.yaml",
    "paper_risk_limits.yaml",
    "options_risk_limits.yaml",
    "shared_risk_limits.yaml",
    "execution_costs.yaml",
    "options_execution_costs.yaml",
    "equity_universe.yaml",
    "options_universe.yaml",
)
_STRATEGY_SOURCE_FILES = (
    "scripts/agents/ai_instrument_allocator_team.py",
    "scripts/decision/instrument_allocator.py",
    "scripts/decision/signed_return_signal.py",
    "scripts/discovery/ai_instrument_allocator_pipeline.py",
    "scripts/exit/position_mandates.py",
    "scripts/options/fill_model.py",
    "scripts/options/paper_broker.py",
    "scripts/options/risk_gate.py",
    "scripts/simulation/fill_model.py",
    "scripts/simulation/fill_transaction.py",
    "scripts/simulation/paper_broker.py",
)
_EQUITY_FIELDS = ("ticker", "asof", "bid", "ask")
_OHLCV_FIELDS = ("timestamp", "open", "high", "low", "close", "volume")
_OPTION_CHAIN_FIELDS = ("underlying", "asof", "contracts")
_OPTION_CONTRACT_FIELDS = (
    "option_id",
    "chain_id",
    "underlying",
    "option_type",
    "strike_price",
    "expiration_date",
    "bid",
    "ask",
    "implied_volatility",
    "delta",
    "gamma",
    "theta",
    "vega",
    "volume",
    "open_interest",
    "updated_at",
)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_hash(value: Any) -> str:
    rendered = json.dumps(value, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _field_path(parent: str, child: str) -> str:
    return f"{parent}.{child}" if parent else child


def _observation_times(
    value: Any,
    *,
    parent: str = "",
) -> Iterable[tuple[str, Any]]:
    if isinstance(value, dict):
        for key, child in value.items():
            path = _field_path(parent, str(key))
            if key in _OBSERVATION_TIME_FIELDS and child is not None:
                yield path, child
            else:
                yield from _observation_times(child, parent=path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _observation_times(child, parent=f"{parent}[{index}]")


def validate_point_in_time_snapshot(
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Validate that every observation was visible by the frozen cutoff."""
    raw_cutoff = snapshot.get("data_cutoff_time") or snapshot.get("decision_time")
    if raw_cutoff is None:
        return {
            "valid": False,
            "cutoff": None,
            "violation_count": 1,
            "violations": [
                {
                    "field": "data_cutoff_time",
                    "observed_at": None,
                    "reason": "missing_cutoff",
                }
            ],
        }
    try:
        cutoff = parse_ts(str(raw_cutoff))
    except (TypeError, ValueError):
        return {
            "valid": False,
            "cutoff": str(raw_cutoff),
            "violation_count": 1,
            "violations": [
                {
                    "field": "data_cutoff_time",
                    "observed_at": str(raw_cutoff),
                    "reason": "invalid_cutoff",
                }
            ],
        }

    violations: list[dict[str, Any]] = []
    for field, raw_observed in _observation_times(snapshot):
        try:
            observed = parse_ts(str(raw_observed))
        except (TypeError, ValueError):
            violations.append(
                {
                    "field": field,
                    "observed_at": str(raw_observed),
                    "reason": "invalid_observation_timestamp",
                }
            )
            continue
        if observed > cutoff:
            violations.append(
                {
                    "field": field,
                    "observed_at": observed.isoformat(),
                    "reason": "observation_after_decision_cutoff",
                }
            )
    return {
        "valid": not violations,
        "cutoff": cutoff.isoformat(),
        "violation_count": len(violations),
        "violations": violations,
    }


def build_validation_manifest(
    project_root: str | Path,
    *,
    data_cutoff: str,
    model_id: str,
    dataset_paths: Iterable[str | Path] = (),
    source_revision: str | None = None,
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    cutoff = parse_ts(data_cutoff).isoformat()
    strategy_profiles = yaml.safe_load(
        (root / "config" / "strategy_profiles.yaml").read_text(encoding="utf-8")
    )
    profile = dict(strategy_profiles.get(_STRATEGY, {}))

    prompt_hashes = {
        name: _sha256_file(root / "scripts" / "llm" / "prompts" / name)
        for name in _PROMPT_FILES
    }
    schema_hashes = {
        name: _sha256_file(root / "schemas" / name) for name in _SCHEMA_FILES
    }
    schema_hashes["scripts/llm/schemas.py"] = _sha256_file(
        root / "scripts" / "llm" / "schemas.py"
    )
    config_hashes = {
        name: _sha256_file(root / "config" / name)
        for name in _CONFIG_FILES
        if (root / "config" / name).is_file()
    }
    strategy_source_hashes = {
        name: _sha256_file(root / name)
        for name in _STRATEGY_SOURCE_FILES
        if (root / name).is_file()
    }
    strategy_digest = _canonical_hash(strategy_source_hashes)
    datasets = {
        str(Path(path).resolve()): _sha256_file(Path(path).resolve())
        for path in dataset_paths
    }
    return {
        "strategy": _STRATEGY,
        "strategy_version": f"{_STRATEGY}@sha256:{strategy_digest}",
        "source_revision": source_revision or "not_recorded",
        "prompt_version": str(profile.get("prompt_version") or "not_recorded"),
        "prompt_hashes": prompt_hashes,
        "schema_version": VALIDATION_SCHEMA_VERSION,
        "schema_hashes": schema_hashes,
        "config_hashes": config_hashes,
        "strategy_source_hashes": strategy_source_hashes,
        "model_id": model_id,
        "data_cutoff": cutoff,
        "dataset_hashes": datasets,
        "manifest_hash": _canonical_hash(
            {
                "strategy": strategy_digest,
                "prompts": prompt_hashes,
                "schemas": schema_hashes,
                "configs": config_hashes,
                "model_id": model_id,
                "data_cutoff": cutoff,
                "datasets": datasets,
            }
        ),
    }


def _missing_value(value: Any) -> bool:
    return value is None or value == ""


def assess_market_data_completeness(
    *,
    equity_rows: list[dict[str, Any]],
    option_chain_snapshots: list[dict[str, Any]],
) -> dict[str, Any]:
    equity_missing: Counter[str] = Counter()
    if not equity_rows:
        equity_missing["dataset.equity_rows"] += 1
    for row in equity_rows:
        for field in _EQUITY_FIELDS:
            if _missing_value(row.get(field)):
                equity_missing[field] += 1
        ohlcv = row.get("ohlcv")
        if not isinstance(ohlcv, dict):
            equity_missing["ohlcv"] += 1
            continue
        for field in _OHLCV_FIELDS:
            if _missing_value(ohlcv.get(field)):
                equity_missing[f"ohlcv.{field}"] += 1
        if ohlcv.get("corporate_action_safe") is not True:
            equity_missing["ohlcv.corporate_action_safe=true"] += 1

    option_missing: Counter[str] = Counter()
    if not option_chain_snapshots:
        option_missing["dataset.option_chain_snapshots"] += 1
    for chain in option_chain_snapshots:
        for field in _OPTION_CHAIN_FIELDS:
            if _missing_value(chain.get(field)):
                option_missing[field] += 1
        contracts = chain.get("contracts")
        if not isinstance(contracts, list) or not contracts:
            option_missing["contracts[]"] += 1
            continue
        for contract in contracts:
            if not isinstance(contract, dict):
                option_missing["contracts[].object"] += 1
                continue
            for field in _OPTION_CONTRACT_FIELDS:
                if _missing_value(contract.get(field)):
                    option_missing[f"contracts[].{field}"] += 1

    equity_ready = not equity_missing
    options_ready = not option_missing
    allowed_claims: list[str] = []
    if equity_ready:
        allowed_claims.append("equity_backtest")
    allowed_claims.append("synthetic_option_sensitivity")
    if options_ready:
        allowed_claims.append("executable_option_pnl")
    return {
        "equity": {
            "row_count": len(equity_rows),
            "executable_backtest_ready": equity_ready,
            "missing_fields": dict(sorted(equity_missing.items())),
        },
        "options": {
            "chain_snapshot_count": len(option_chain_snapshots),
            "executable_backtest_ready": options_ready,
            "executable_pnl_claim_allowed": options_ready,
            "synthetic_sensitivity_allowed": True,
            "missing_fields": dict(sorted(option_missing.items())),
        },
        "allowed_claims": allowed_claims,
    }


def build_walk_forward_partitions(
    records: list[dict[str, Any]],
    *,
    horizon: str,
    development_end: str,
    calibration_end: str,
    holdout_end: str,
    mode: str,
    rolling_train_size: int | None = None,
) -> dict[str, Any]:
    if mode not in {"expanding", "rolling"}:
        raise ValueError("walk-forward mode must be expanding or rolling")
    if mode == "rolling" and (
        rolling_train_size is None or rolling_train_size <= 0
    ):
        raise ValueError("rolling_train_size must be positive in rolling mode")
    development_cutoff = parse_ts(development_end)
    calibration_cutoff = parse_ts(calibration_end)
    holdout_cutoff = parse_ts(holdout_end)
    if not development_cutoff < calibration_cutoff < holdout_cutoff:
        raise ValueError("walk-forward boundaries must be strictly increasing")

    same_horizon = sorted(
        (record for record in records if record.get("horizon") == horizon),
        key=lambda record: parse_ts(str(record["decision_time"])),
    )
    partitions = {
        "development": [],
        "calibration": [],
        "final_holdout": [],
    }
    for record in same_horizon:
        decision = parse_ts(str(record["decision_time"]))
        if decision <= development_cutoff:
            partitions["development"].append(record)
        elif decision <= calibration_cutoff:
            partitions["calibration"].append(record)
        elif decision <= holdout_cutoff:
            partitions["final_holdout"].append(record)

    def folds(
        tests: list[dict[str, Any]],
        allowed_training_partitions: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        pool = [
            record
            for name in allowed_training_partitions
            for record in partitions[name]
        ]
        result: list[dict[str, Any]] = []
        for test in tests:
            test_decision = parse_ts(str(test["decision_time"]))
            training = sorted(
                (
                    record
                    for record in pool
                    if parse_ts(str(record["decision_time"])) < test_decision
                    and parse_ts(str(record["label_matured_at"])) <= test_decision
                ),
                key=lambda record: parse_ts(str(record["decision_time"])),
            )
            if mode == "rolling":
                training = training[-int(rolling_train_size or 0) :]
            result.append(
                {
                    "horizon": horizon,
                    "mode": mode,
                    "training_cutoff_time": test_decision.isoformat(),
                    "training_records": training,
                    "test_record": test,
                }
            )
        return result

    calibration_folds = folds(partitions["calibration"], ("development",))
    holdout_folds = folds(
        partitions["final_holdout"],
        ("development", "calibration"),
    )
    membership = {
        name: {str(record.get("record_id")) for record in values}
        for name, values in partitions.items()
    }
    overlap_count = sum(
        len(membership[left] & membership[right])
        for left, right in (
            ("development", "calibration"),
            ("development", "final_holdout"),
            ("calibration", "final_holdout"),
        )
    )
    all_folds = [*calibration_folds, *holdout_folds]
    future_or_unmatured = sum(
        parse_ts(str(record["decision_time"])) >= parse_ts(fold["training_cutoff_time"])
        or parse_ts(str(record["label_matured_at"]))
        > parse_ts(fold["training_cutoff_time"])
        for fold in all_folds
        for record in fold["training_records"]
    )
    holdout_ids = membership["final_holdout"]
    holdout_used = sum(
        str(record.get("record_id")) in holdout_ids
        for fold in all_folds
        for record in fold["training_records"]
    )
    return {
        "horizon": horizon,
        "mode": mode,
        "boundaries": {
            "development_end": development_cutoff.isoformat(),
            "calibration_end": calibration_cutoff.isoformat(),
            "holdout_end": holdout_cutoff.isoformat(),
        },
        "partitions": partitions,
        "calibration_folds": calibration_folds,
        "holdout_folds": holdout_folds,
        "leakage_checks": {
            "partition_overlap_count": overlap_count,
            "future_or_unmatured_training_count": future_or_unmatured,
            "holdout_used_for_training_count": holdout_used,
        },
    }


def funnel_with_conversion(counts: dict[str, int]) -> dict[str, Any]:
    values = {key: max(0, int(value)) for key, value in counts.items()}

    def rate(numerator: str, denominator: str) -> float | None:
        bottom = values.get(denominator, 0)
        if bottom <= 0:
            return None
        return round(values.get(numerator, 0) / bottom, 8)

    return {
        "counts": values,
        "conversion_rates": {
            "candidate_to_structured_decision": rate(
                "structured_decisions", "candidates"
            ),
            "proposal_to_allocation": rate("allocations", "proposals"),
            "allocation_to_order": rate("paper_orders", "allocations"),
            "order_to_fill": rate("paper_fills", "paper_orders"),
        },
        "outcome_rates": {
            "watch": rate("watch", "structured_decisions"),
            "no_trade": rate("no_trade", "structured_decisions"),
            "proposal": rate("proposals", "structured_decisions"),
        },
    }
