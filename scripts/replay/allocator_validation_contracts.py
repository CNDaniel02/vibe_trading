from __future__ import annotations

from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import subprocess
from typing import Any, Iterable

import yaml

from scripts.core.models import parse_ts


VALIDATION_SCHEMA_VERSION = "allocator-historical-validation-v1"
_STRATEGY = "ai_instrument_allocator_v1"
_OBSERVATION_TIME_FIELDS = {
    "asof",
    "calculated_at",
    "event_at",
    "event_time",
    "event_timestamp",
    "first_seen_at",
    "nav_calculated_at",
    "observed_at",
    "published_at",
    "quote_seen_at",
    "quotes_observed_at",
    "retrieved_at",
    "timestamp",
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
    "allocator_historical_validation_report.schema.json",
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
    "historical_validation.yaml",
)
_STRATEGY_SOURCE_FILES = (
    "scripts/agents/ai_instrument_allocator_team.py",
    "scripts/decision/instrument_allocator.py",
    "scripts/decision/signed_return_signal.py",
    "scripts/discovery/ai_instrument_allocator_pipeline.py",
    "scripts/discovery/evidence_store.py",
    "scripts/exit/position_mandates.py",
    "scripts/options/fill_model.py",
    "scripts/options/paper_broker.py",
    "scripts/options/risk_gate.py",
    "scripts/simulation/fill_model.py",
    "scripts/simulation/fill_transaction.py",
    "scripts/simulation/paper_broker.py",
    "scripts/replay/allocator_functional_replay.py",
    "scripts/replay/allocator_historical_replay.py",
    "scripts/replay/allocator_validation_contracts.py",
    "scripts/replay/allocator_validation_report.py",
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


def detect_source_revision(project_root: str | Path) -> str:
    """Bind a manifest to HEAD and make worktree dirtiness explicit."""
    root = Path(project_root).resolve()
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return "not_recorded"
    revision = head.stdout.strip() or "not_recorded"
    dirty = status.stdout.strip()
    if dirty:
        digest = hashlib.sha256(dirty.encode("utf-8")).hexdigest()[:16]
        return f"{revision}+dirty:{digest}"
    return revision


def _canonical_hash(value: Any) -> str:
    rendered = json.dumps(value, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def verify_immutable_snapshot_file(
    path: str | Path,
    *,
    expected_hash: str | None = None,
    allowed_root: str | Path | None = None,
) -> dict[str, Any]:
    snapshot_path = Path(path).resolve()
    if allowed_root is not None:
        root = Path(allowed_root).resolve()
        if snapshot_path != root and root not in snapshot_path.parents:
            raise ValueError(f"snapshot path escapes allowed root: {snapshot_path}")
    value = json.loads(snapshot_path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"immutable snapshot must be an object: {snapshot_path}")
    expected = str(value.get("snapshot_hash") or "")
    unsigned = dict(value)
    unsigned.pop("snapshot_hash", None)
    actual = _canonical_hash(unsigned)
    if (
        not expected
        or expected != actual
        or expected[:12] not in snapshot_path.name
        or (expected_hash is not None and expected != str(expected_hash))
    ):
        raise ValueError(f"snapshot hash mismatch: {snapshot_path}")
    return value


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
    *,
    replay_asof: str | None = None,
    allow_decision_time_cutoff: bool = False,
) -> dict[str, Any]:
    """Validate that every observation was visible by the frozen cutoff."""
    raw_cutoff = snapshot.get("data_cutoff_time")
    if raw_cutoff is None and allow_decision_time_cutoff:
        raw_cutoff = snapshot.get("decision_time")
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
    if replay_asof is not None and cutoff > parse_ts(replay_asof):
        violations.append(
            {
                "field": "data_cutoff_time",
                "observed_at": cutoff.isoformat(),
                "reason": "decision_cutoff_after_replay_asof",
            }
        )
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
    provider_id: str = "not_recorded",
    precomputed_dataset_hashes: dict[str, str] | None = None,
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
    datasets.update(
        {
            str(Path(path).resolve()): str(digest)
            for path, digest in (precomputed_dataset_hashes or {}).items()
        }
    )
    prompt_version = str(profile.get("prompt_version") or "not_recorded")
    source_version = source_revision or detect_source_revision(root)
    manifest_payload = {
        "source_revision": source_version,
        "strategy": strategy_digest,
        "prompt_version": prompt_version,
        "prompts": prompt_hashes,
        "schema_version": VALIDATION_SCHEMA_VERSION,
        "schemas": schema_hashes,
        "configs": config_hashes,
        "provider_id": provider_id,
        "model_id": model_id,
        "data_cutoff": cutoff,
        "datasets": datasets,
    }
    return {
        "strategy": _STRATEGY,
        "strategy_version": f"{_STRATEGY}@sha256:{strategy_digest}",
        "source_revision": source_version,
        "prompt_version": prompt_version,
        "prompt_hashes": prompt_hashes,
        "schema_version": VALIDATION_SCHEMA_VERSION,
        "schema_hashes": schema_hashes,
        "config_hashes": config_hashes,
        "strategy_source_hashes": strategy_source_hashes,
        "provider_id": provider_id,
        "model_id": model_id,
        "data_cutoff": cutoff,
        "dataset_hashes": datasets,
        "manifest_hash": _canonical_hash(manifest_payload),
    }


def _missing_value(value: Any) -> bool:
    return value is None or value == ""


def _invalid_number(value: Any) -> bool:
    return isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)


def _invalid_timestamp(value: Any) -> bool:
    if _missing_value(value):
        return True
    try:
        parse_ts(str(value))
    except (TypeError, ValueError):
        return True
    return False


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
        for field in ("bid", "ask"):
            if not _missing_value(row.get(field)) and _invalid_number(row.get(field)):
                equity_missing[f"{field}.finite"] += 1
        if (
            not _invalid_number(row.get("bid"))
            and not _invalid_number(row.get("ask"))
            and (row["bid"] < 0 or row["ask"] <= 0 or row["bid"] > row["ask"])
        ):
            equity_missing["bid_ask.valid"] += 1
        if _invalid_timestamp(row.get("asof")):
            equity_missing["asof.valid_timestamp"] += 1
        ohlcv = row.get("ohlcv")
        if not isinstance(ohlcv, dict):
            equity_missing["ohlcv"] += 1
            continue
        for field in _OHLCV_FIELDS:
            if _missing_value(ohlcv.get(field)):
                equity_missing[f"ohlcv.{field}"] += 1
        if _invalid_timestamp(ohlcv.get("timestamp")):
            equity_missing["ohlcv.timestamp.valid"] += 1
        for field in ("open", "high", "low", "close", "volume"):
            if not _missing_value(ohlcv.get(field)) and _invalid_number(
                ohlcv.get(field)
            ):
                equity_missing[f"ohlcv.{field}.finite"] += 1
        prices = [ohlcv.get(field) for field in ("open", "high", "low", "close")]
        if all(not _invalid_number(value) for value in prices):
            open_price, high, low, close = (float(value) for value in prices)
            if (
                low <= 0
                or high < max(open_price, close, low)
                or low > min(open_price, close, high)
            ):
                equity_missing["ohlcv.price_relationships.valid"] += 1
        if not _invalid_number(ohlcv.get("volume")) and float(ohlcv["volume"]) < 0:
            equity_missing["ohlcv.volume.nonnegative"] += 1
        if ohlcv.get("corporate_action_safe") is not True:
            equity_missing["ohlcv.corporate_action_safe=true"] += 1
        if ohlcv.get("coverage_complete") is not True:
            equity_missing["ohlcv.coverage_complete=true"] += 1

    option_missing: Counter[str] = Counter()
    if not option_chain_snapshots:
        option_missing["dataset.option_chain_snapshots"] += 1
    for chain in option_chain_snapshots:
        for field in _OPTION_CHAIN_FIELDS:
            if _missing_value(chain.get(field)):
                option_missing[field] += 1
        if chain.get("chain_complete") is not True:
            option_missing["chain_complete=true"] += 1
        chain_asof = None
        if _invalid_timestamp(chain.get("asof")):
            option_missing["asof.valid_timestamp"] += 1
        else:
            chain_asof = parse_ts(str(chain["asof"]))
        coverage = chain.get("chain_completeness")
        if not isinstance(coverage, dict):
            option_missing["chain_completeness"] += 1
        else:
            expected_count = coverage.get("expected_contract_count")
            received_count = coverage.get("received_contract_count")
            if (
                not isinstance(expected_count, int)
                or isinstance(expected_count, bool)
                or not isinstance(received_count, int)
                or isinstance(received_count, bool)
                or expected_count <= 0
                or received_count != expected_count
            ):
                option_missing["chain_completeness.contract_count_match"] += 1
            if coverage.get("expirations_complete") is not True:
                option_missing["chain_completeness.expirations_complete=true"] += 1
            if coverage.get("strikes_complete") is not True:
                option_missing["chain_completeness.strikes_complete=true"] += 1
        provenance = chain.get("coverage_provenance")
        if not isinstance(provenance, dict):
            option_missing["coverage_provenance"] += 1
        else:
            if not str(provenance.get("source") or "").strip():
                option_missing["coverage_provenance.source"] += 1
            if not (
                str(provenance.get("request_id") or "").strip()
                or str(provenance.get("dataset_hash") or "").strip()
            ):
                option_missing[
                    "coverage_provenance.request_id_or_dataset_hash"
                ] += 1
            if provenance.get("pagination_complete") is not True:
                option_missing[
                    "coverage_provenance.pagination_complete=true"
                ] += 1
            if _invalid_timestamp(provenance.get("captured_at")):
                option_missing["coverage_provenance.captured_at.valid"] += 1
            query = provenance.get("query")
            if not isinstance(query, dict):
                option_missing["coverage_provenance.query"] += 1
            else:
                if str(query.get("underlying") or "").upper() != str(
                    chain.get("underlying") or ""
                ).upper():
                    option_missing[
                        "coverage_provenance.query.underlying_matches_chain"
                    ] += 1
                for field in (
                    "expiration_start",
                    "expiration_end",
                    "strike_min",
                    "strike_max",
                ):
                    if _missing_value(query.get(field)):
                        option_missing[f"coverage_provenance.query.{field}"] += 1
        contracts = chain.get("contracts")
        if not isinstance(contracts, list) or not contracts:
            option_missing["contracts[]"] += 1
            continue
        if isinstance(coverage, dict) and coverage.get(
            "received_contract_count"
        ) != len(contracts):
            option_missing[
                "chain_completeness.received_count_matches_contracts"
            ] += 1
        for contract in contracts:
            if not isinstance(contract, dict):
                option_missing["contracts[].object"] += 1
                continue
            for field in _OPTION_CONTRACT_FIELDS:
                if _missing_value(contract.get(field)):
                    option_missing[f"contracts[].{field}"] += 1
            for field in (
                "strike_price",
                "bid",
                "ask",
                "implied_volatility",
                "delta",
                "gamma",
                "theta",
                "vega",
                "volume",
                "open_interest",
            ):
                if not _missing_value(contract.get(field)) and _invalid_number(
                    contract.get(field)
                ):
                    option_missing[f"contracts[].{field}.finite"] += 1
            for field in ("updated_at",):
                if _invalid_timestamp(contract.get(field)):
                    option_missing[f"contracts[].{field}.valid"] += 1
            try:
                expiration = parse_ts(f"{contract.get('expiration_date')}T00:00:00+00:00")
            except (TypeError, ValueError):
                option_missing["contracts[].expiration_date.valid"] += 1
            else:
                if chain_asof is not None and expiration.date() < chain_asof.date():
                    option_missing["contracts[].expiration_not_expired"] += 1
            if chain_asof is not None and not _invalid_timestamp(contract.get("updated_at")):
                if parse_ts(str(contract["updated_at"])) > chain_asof:
                    option_missing["contracts[].updated_at<=chain.asof"] += 1
            if str(contract.get("underlying") or "").upper() != str(
                chain.get("underlying") or ""
            ).upper():
                option_missing["contracts[].underlying_matches_chain"] += 1
            if contract.get("option_type") not in {"call", "put"}:
                option_missing["contracts[].option_type.valid"] += 1
            for field in ("strike_price", "ask"):
                if not _invalid_number(contract.get(field)) and float(contract[field]) <= 0:
                    option_missing[f"contracts[].{field}.positive"] += 1
            for field in (
                "bid",
                "implied_volatility",
                "gamma",
                "vega",
                "volume",
                "open_interest",
            ):
                if not _invalid_number(contract.get(field)) and float(contract[field]) < 0:
                    option_missing[f"contracts[].{field}.nonnegative"] += 1
            if not _invalid_number(contract.get("delta")) and not -1 <= float(
                contract["delta"]
            ) <= 1:
                option_missing["contracts[].delta.range"] += 1
            if (
                not _invalid_number(contract.get("bid"))
                and not _invalid_number(contract.get("ask"))
                and (
                    contract["bid"] < 0
                    or contract["ask"] <= 0
                    or contract["bid"] > contract["ask"]
                )
            ):
                option_missing["contracts[].bid_ask.valid"] += 1

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
            "coverage_provenance_required": True,
            "coverage_provenance_structurally_valid": options_ready,
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

    record_ids = [str(record.get("record_id") or "") for record in records]
    if any(not record_id for record_id in record_ids):
        raise ValueError("walk-forward records require non-empty record_id")
    if len(record_ids) != len(set(record_ids)):
        raise ValueError("walk-forward record_id values must be unique")

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


def assess_walk_forward_readiness(
    data_completeness: dict[str, Any],
    *,
    labeled_records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Describe what can be tested without turning diagnostics into PnL claims."""
    records = labeled_records or []
    equity = data_completeness.get("equity")
    options = data_completeness.get("options")
    equity = equity if isinstance(equity, dict) else {}
    options = options if isinstance(options, dict) else {}
    blockers: list[str] = []
    limitations: list[str] = []
    if not records:
        blockers.append("missing_point_in_time_labeled_outcome_dataset")
    if equity.get("executable_backtest_ready") is not True:
        blockers.append("incomplete_equity_bid_ask_or_corporate_action_safe_ohlcv")
    if options.get("executable_backtest_ready") is not True:
        limitations.append("incomplete_historical_option_chain")

    horizons = sorted(
        {
            str(record.get("horizon"))
            for record in records
            if record.get("horizon")
        }
    )
    status = (
        "blocked"
        if blockers
        else "ready_for_partitioning"
        if not limitations
        else "equity_ready_options_sensitivity_only"
    )
    return {
        "status": status,
        "supported_modes": ["expanding", "rolling"],
        "partition_contract_requires_separation": True,
        "partition_contract_requires_matured_labels": True,
        "development_calibration_holdout_separated": False,
        "matured_labels_only": False,
        "labeled_dataset_provided": bool(records),
        "labeled_record_count": len(records),
        "horizons": horizons,
        "actual_partitions_run": False,
        "leakage_checks_run": False,
        "equity_executable_backtest_ready": bool(
            equity.get("executable_backtest_ready") is True and records
        ),
        "option_executable_pnl_ready": bool(
            options.get("executable_backtest_ready") is True and records
        ),
        "synthetic_option_sensitivity_allowed": True,
        "blockers": blockers,
        "limitations": limitations,
        "claim_boundary": (
            "No historical profitability claim is allowed until labeled records are "
            "partitioned and out-of-sample holdout evaluation is complete."
        ),
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
