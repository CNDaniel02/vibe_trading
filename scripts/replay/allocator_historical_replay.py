from __future__ import annotations

import argparse
from collections import Counter
from datetime import timedelta
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

import yaml

from scripts.core.models import parse_ts
from scripts.replay.allocator_policy_replay import run_allocator_policy_replay
from scripts.replay.allocator_validation_contracts import (
    assess_market_data_completeness,
    build_validation_manifest,
    funnel_with_conversion,
    validate_point_in_time_snapshot,
    verify_immutable_snapshot_file,
)


_NAMESPACE = "ai_instrument_allocator_v1"


def _read_jsonl_snapshot(
    path: Path,
) -> tuple[list[dict[str, Any]], str | None, int]:
    if not path.is_file():
        return [], None, 0
    payload = path.read_bytes()
    rows: list[dict[str, Any]] = []
    parse_errors = 0
    for line in payload.decode("utf-8-sig").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            parse_errors += 1
            continue
        if isinstance(value, dict):
            rows.append(value)
        else:
            parse_errors += 1
    return rows, hashlib.sha256(payload).hexdigest(), parse_errors


def _record_time(record: dict[str, Any]):
    fill = record.get("fill") if isinstance(record.get("fill"), dict) else {}
    for raw in (
        fill.get("filled_at"),
        record.get("decision_time"),
        record.get("data_cutoff_time"),
        record.get("asof"),
        record.get("ts"),
    ):
        if raw:
            try:
                return parse_ts(str(raw))
            except (TypeError, ValueError):
                continue
    return None


def _recent(
    records: Iterable[dict[str, Any]],
    *,
    cutoff,
    asof,
) -> list[dict[str, Any]]:
    return [
        record
        for record in records
        if (observed := _record_time(record)) is not None
        and cutoff <= observed <= asof
    ]


def _path_key(path: str | Path) -> str:
    return str(Path(path).resolve()).lower()


def _candidate_records(cycles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for cycle in cycles:
        funnel = cycle.get("funnel")
        if not isinstance(funnel, dict):
            continue
        values = funnel.get("candidate_records")
        if isinstance(values, list):
            rows.extend(item for item in values if isinstance(item, dict))
    return rows


def _snapshot_ticker(snapshot: dict[str, Any]) -> str:
    payload = snapshot.get("payload") if isinstance(snapshot.get("payload"), dict) else {}
    candidate = payload.get("candidate") if isinstance(payload.get("candidate"), dict) else {}
    return str(candidate.get("ticker") or payload.get("ticker") or "").upper()


def _market_parts(
    snapshot: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, list[dict[str, Any]]]:
    payload = snapshot.get("payload") if isinstance(snapshot.get("payload"), dict) else {}
    candidate = payload.get("candidate") if isinstance(payload.get("candidate"), dict) else {}
    context = candidate.get("market_context") if isinstance(candidate.get("market_context"), dict) else {}
    agent = payload.get("agent_snapshot") if isinstance(payload.get("agent_snapshot"), dict) else {}
    market = agent.get("market_data") if isinstance(agent.get("market_data"), dict) else {}
    quote = context.get("quote") if isinstance(context.get("quote"), dict) else None
    if quote is None and isinstance(market.get("quote"), dict):
        quote = market["quote"]
    ohlcv = context.get("ohlcv") if isinstance(context.get("ohlcv"), dict) else None
    if ohlcv is None and isinstance(market.get("ohlcv"), dict):
        ohlcv = market["ohlcv"]
    raw_chain = context.get("option_chain") or market.get("option_chain") or []
    option_chains: list[dict[str, Any]] = []
    if isinstance(raw_chain, dict):
        option_chains = [raw_chain]
    elif isinstance(raw_chain, list) and raw_chain:
        if all(isinstance(item, dict) and "contracts" in item for item in raw_chain):
            option_chains = [dict(item) for item in raw_chain]
        else:
            option_chains = [
                {
                    "underlying": _snapshot_ticker(snapshot),
                    "asof": snapshot.get("decision_time"),
                    "contracts": [item for item in raw_chain if isinstance(item, dict)],
                }
            ]
    return quote, ohlcv, option_chains


def _configured_model(root: Path) -> tuple[str, str]:
    path = root / "config" / "llm.yaml"
    if not path.is_file():
        return "not_recorded", "not_recorded"
    value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    api = value.get("api") if isinstance(value.get("api"), dict) else {}
    return (
        str(value.get("provider") or "not_recorded"),
        str(api.get("model") or value.get("model") or "not_recorded"),
    )


def _reference_key(
    record: dict[str, Any],
    *,
    snapshot_root: Path,
    snapshots_by_path: dict[str, dict[str, Any]],
    rejections: Counter[str],
) -> str | None:
    reference = record.get("evidence_snapshot")
    if not isinstance(reference, dict):
        rejections["lineage: missing_snapshot_reference"] += 1
        return None
    raw_path = reference.get("path")
    expected_hash = str(reference.get("snapshot_hash") or "")
    if not raw_path or not expected_hash:
        rejections["lineage: incomplete_snapshot_reference"] += 1
        return None
    resolved = Path(str(raw_path)).resolve()
    if resolved != snapshot_root and snapshot_root not in resolved.parents:
        rejections["lineage: snapshot_path_outside_source_root"] += 1
        return None
    key = _path_key(resolved)
    snapshot_row = snapshots_by_path.get(key)
    if snapshot_row is None:
        rejections["lineage: snapshot_not_in_window_or_invalid"] += 1
        return None
    if str(snapshot_row["snapshot"].get("snapshot_hash") or "") != expected_hash:
        rejections["lineage: snapshot_reference_hash_mismatch"] += 1
        return None
    return key


def run_natural_strict_replay(
    root: str | Path,
    *,
    hours: int = 48,
    asof: str | None = None,
    source_revision: str | None = None,
    project_root: str | Path | None = None,
) -> dict[str, Any]:
    """Replay recorded natural outcomes without model, broker, or state writes."""
    root_path = Path(root).resolve()
    source_root = (
        Path(project_root).resolve()
        if project_root is not None
        else Path(__file__).resolve().parents[2]
    )
    if asof is None:
        raise ValueError("natural strict replay requires an explicit asof cutoff")
    current = parse_ts(asof)
    log_dir = root_path / "logs" / "strategy_sleeves" / _NAMESPACE
    captured_hashes: dict[str, str] = {}
    capture_parse_errors: Counter[str] = Counter()

    def capture(name: str) -> list[dict[str, Any]]:
        path = log_dir / name
        rows, digest, parse_errors = _read_jsonl_snapshot(path)
        if digest is not None:
            captured_hashes[str(path.resolve())] = digest
        if parse_errors:
            capture_parse_errors[name] += parse_errors
        return rows

    cycles_all = capture("cycles.jsonl")
    decisions_all = capture("decisions.jsonl")
    allocations_all = capture("allocations.jsonl")
    fills_all = [
        *capture("paper_fills.jsonl"),
        *capture("paper_option_fills.jsonl"),
    ]

    snapshot_rows: list[dict[str, Any]] = []
    invalid_snapshot_files: list[dict[str, str]] = []
    snapshot_root = root_path / "logs" / "ai_instrument_allocator_snapshots"
    snapshot_paths = sorted(
        snapshot_root.glob("*.json")
    )
    for path in snapshot_paths:
        try:
            snapshot = verify_immutable_snapshot_file(
                path,
                allowed_root=snapshot_root,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            invalid_snapshot_files.append({"path": str(path.resolve()), "error": str(exc)})
            continue
        snapshot_rows.append(
            {
                "path": str(path.resolve()),
                "path_key": _path_key(path),
                "snapshot": snapshot,
                "decision_time": parse_ts(str(snapshot["decision_time"])),
                "ticker": _snapshot_ticker(snapshot),
            }
        )
    window_hours = max(1, int(hours))
    window_start = current - timedelta(hours=window_hours)
    snapshots = [
        item
        for item in snapshot_rows
        if window_start <= item["decision_time"] <= current
    ]
    cycles = _recent(cycles_all, cutoff=window_start, asof=current)
    decisions = _recent(decisions_all, cutoff=window_start, asof=current)
    allocations = _recent(allocations_all, cutoff=window_start, asof=current)
    fills = _recent(fills_all, cutoff=window_start, asof=current)

    rejections: Counter[str] = Counter()
    for name, count in capture_parse_errors.items():
        rejections[f"input_parse_error: {name}"] += count
    source_violations: list[dict[str, Any]] = []
    invalid_time_snapshot_paths: set[str] = set()
    admissible_paths: set[str] = set()
    equity_rows: list[dict[str, Any]] = []
    option_chains: list[dict[str, Any]] = []
    for item in snapshots:
        snapshot = item["snapshot"]
        validation = validate_point_in_time_snapshot(
            snapshot,
            replay_asof=current.isoformat(),
        )
        quote, ohlcv, chains = _market_parts(snapshot)
        equity_row = {
            "ticker": item["ticker"],
            "asof": quote.get("asof") if quote else None,
            "bid": quote.get("bid") if quote else None,
            "ask": quote.get("ask") if quote else None,
            "ohlcv": ohlcv,
        }
        if not validation["valid"]:
            invalid_time_snapshot_paths.add(item["path_key"])
            source_violations.extend(
                {"snapshot": item["path"], **violation}
                for violation in validation["violations"]
            )
            for reason in {
                str(violation["reason"]) for violation in validation["violations"]
            }:
                rejections[f"point_in_time: {reason}"] += 1
            continue
        if quote is None:
            rejections["missing_historical_quote"] += 1
            continue
        admissible_paths.add(item["path_key"])
        equity_rows.append(equity_row)
        option_chains.extend(chains)

    snapshots_by_path = {item["path_key"]: item for item in snapshots}

    def admissible_reference(record: dict[str, Any]) -> str | None:
        key = _reference_key(
            record,
            snapshot_root=snapshot_root.resolve(),
            snapshots_by_path=snapshots_by_path,
            rejections=rejections,
        )
        return key if key in admissible_paths else None

    candidates = _candidate_records(cycles)
    candidate_links = [
        (record, key)
        for record in candidates
        if (key := admissible_reference(record)) is not None
    ]
    candidate_paths = {key for _, key in candidate_links}
    ranking_paths = {
        key for record, key in candidate_links if bool(record.get("ranking_entered"))
    }
    deep_paths = {
        key for record, key in candidate_links if bool(record.get("deep_research"))
    }
    decision_by_path: dict[str, dict[str, Any]] = {}
    for record in decisions:
        key = admissible_reference(record)
        if key is None:
            continue
        if key not in deep_paths:
            rejections["lineage: decision_without_deep_research"] += 1
            continue
        decision_by_path[key] = record
    admissible_decisions = list(decision_by_path.values())
    for decision in admissible_decisions:
        signal = decision.get("signal") if isinstance(decision.get("signal"), dict) else {}
        action = str(signal.get("action") or "no_trade")
        if action == "no_trade":
            reason = str(signal.get("no_trade_reason") or "unspecified")
            rejections[f"model_no_trade: {reason}"] += 1
        elif action == "watch":
            reason = str(signal.get("watch_reason") or "unspecified")
            rejections[f"watch: {reason}"] += 1

    candidate_count = len(candidate_paths)
    ranking_count = len(ranking_paths)
    deep_count = len(deep_paths)
    if not candidates and admissible_decisions:
        rejections["lineage: missing_candidate_records"] += len(admissible_decisions)

    proposal_paths = {
        key
        for key, decision in decision_by_path.items()
        if str(
            (
                decision.get("signal")
                if isinstance(decision.get("signal"), dict)
                else {}
            ).get("action")
            or "no_trade"
        )
        == "propose_trade"
    }

    plan_paths: dict[str, str] = {}
    for cycle in cycles:
        for plan in cycle.get("plans") or []:
            if not isinstance(plan, dict):
                continue
            plan_id = str(plan.get("plan_id") or "")
            path = admissible_reference(plan)
            if path is not None and path not in proposal_paths:
                rejections["lineage: plan_without_proposal"] += 1
                continue
            if plan_id and path:
                plan_paths[plan_id] = path
    admissible_plan_ids = {
        plan_id for plan_id, path in plan_paths.items() if path in admissible_paths
    }

    admissible_allocations: list[dict[str, Any]] = []
    for allocation in allocations:
        if str(allocation.get("plan_id") or "") not in admissible_plan_ids:
            continue
        validation = validate_point_in_time_snapshot(
            allocation,
            replay_asof=current.isoformat(),
        )
        if not validation["valid"]:
            source_violations.extend(
                {"allocation_id": allocation.get("allocation_id"), **violation}
                for violation in validation["violations"]
            )
            for violation in validation["violations"]:
                rejections[f"point_in_time: {violation['reason']}"] += 1
            continue
        admissible_allocations.append(allocation)
        if allocation.get("status") != "selected":
            reason = str(allocation.get("reason") or "unspecified")
            rejections[f"allocation: {reason}"] += 1

    selected_allocations = {
        str(allocation.get("allocation_id") or ""): allocation
        for allocation in admissible_allocations
        if allocation.get("status") == "selected" and allocation.get("allocation_id")
    }
    order_times: dict[str, Any] = {}
    for cycle in cycles:
        for execution in cycle.get("executions") or []:
            if not isinstance(execution, dict):
                continue
            allocation = execution.get("allocation")
            order = execution.get("order")
            if not isinstance(allocation, dict) or not isinstance(order, dict):
                continue
            allocation_id = str(allocation.get("allocation_id") or "")
            admitted_allocation = selected_allocations.get(allocation_id)
            if admitted_allocation is None:
                rejections["lineage: order_without_selected_allocation"] += 1
                continue
            allocation_cutoff = admitted_allocation.get(
                "data_cutoff_time"
            ) or admitted_allocation.get("decision_time")
            order_validation = validate_point_in_time_snapshot(
                {
                    "decision_time": allocation_cutoff,
                    "data_cutoff_time": allocation_cutoff,
                    "order": order,
                },
                replay_asof=current.isoformat(),
            )
            if not order_validation["valid"]:
                for violation in order_validation["violations"]:
                    rejections[f"point_in_time: {violation['reason']}"] += 1
                continue
            order_id = str(order.get("order_id") or "")
            raw_created_at = order.get("created_at") or order.get("submitted_at")
            if not order_id or not raw_created_at:
                rejections["lineage: incomplete_order_identity"] += 1
                continue
            try:
                created_at = parse_ts(str(raw_created_at))
                allocation_time = parse_ts(str(allocation_cutoff))
            except (TypeError, ValueError):
                rejections["lineage: invalid_order_timestamp"] += 1
                continue
            if created_at < allocation_time or created_at > current:
                rejections["lineage: order_timestamp_out_of_sequence"] += 1
                continue
            order_times[order_id] = created_at

    admitted_fill_keys: set[tuple[str, str]] = set()
    for record in fills:
        fill = record.get("fill") if isinstance(record.get("fill"), dict) else record
        order_id = str(fill.get("order_id") or "")
        raw_filled_at = fill.get("filled_at")
        if order_id not in order_times or not raw_filled_at:
            continue
        try:
            filled_at = parse_ts(str(raw_filled_at))
        except (TypeError, ValueError):
            rejections["lineage: invalid_fill_timestamp"] += 1
            continue
        if filled_at < order_times[order_id] or filled_at > current:
            rejections["lineage: fill_timestamp_out_of_sequence"] += 1
            continue
        admitted_fill_keys.add((order_id, filled_at.isoformat()))
    actions = Counter(
        str((record.get("signal") or {}).get("action") or "no_trade")
        for record in admissible_decisions
    )
    counts = {
        "candidates": candidate_count,
        "ranking_input": ranking_count,
        "deep_research": deep_count,
        "structured_decisions": len(admissible_decisions),
        "watch": actions["watch"],
        "no_trade": actions["no_trade"],
        "proposals": actions["propose_trade"],
        "allocations": len(admissible_allocations),
        "selected_instruments": sum(
            allocation.get("status") == "selected"
            for allocation in admissible_allocations
        ),
        "paper_orders": len(order_times),
        "paper_fills": len(admitted_fill_keys),
    }
    strict_funnel = funnel_with_conversion(counts)
    strict_funnel["conversion_rates"].update(
        {
            "candidate_to_ranking": (
                round(ranking_count / candidate_count, 8)
                if candidate_count
                else None
            ),
            "ranking_to_deep_research": (
                round(deep_count / ranking_count, 8) if ranking_count else None
            ),
            "deep_research_to_structured_decision": (
                round(len(admissible_decisions) / deep_count, 8)
                if deep_count
                else None
            ),
        }
    )

    policy_report = run_allocator_policy_replay(
        root_path,
        hours=window_hours,
        asof=current.isoformat(),
    )
    completeness = assess_market_data_completeness(
        equity_rows=equity_rows,
        option_chain_snapshots=option_chains,
    )
    completeness["point_in_time_source_ready"] = not source_violations
    provider_id, model_id = _configured_model(root_path)
    manifest = build_validation_manifest(
        source_root,
        data_cutoff=current.isoformat(),
        model_id=model_id,
        dataset_paths=[Path(item["path"]) for item in snapshots],
        source_revision=source_revision,
        provider_id=provider_id,
        precomputed_dataset_hashes=captured_hashes,
    )
    return {
        "evidence_type": "strict_historical_diagnostic",
        "strategy": _NAMESPACE,
        "window_hours": window_hours,
        "window_started_at": window_start.isoformat(),
        "asof": current.isoformat(),
        "strict_funnel": strict_funnel,
        "rejection_reasons": dict(sorted(rejections.items())),
        "time_validation": {
            "time_violation_count": 0,
            "source_violation_count": len(invalid_time_snapshot_paths),
            "admitted_violation_count": 0,
            "excluded_snapshot_count": len(snapshots) - len(admissible_paths),
            "by_field": dict(
                Counter(str(item.get("field")) for item in source_violations)
            ),
            "examples": source_violations[:10],
        },
        "snapshot_integrity": {
            "checked": len(snapshots) + len(invalid_snapshot_files),
            "valid_hashes": len(snapshots),
            "invalid_hashes": len(invalid_snapshot_files),
            "invalid_examples": invalid_snapshot_files[:10],
        },
        "input_capture": {
            "mode": "single_read_in_memory",
            "jsonl_file_count": len(captured_hashes),
            "jsonl_hashes": dict(sorted(captured_hashes.items())),
            "parse_error_count": sum(capture_parse_errors.values()),
            "parse_errors_by_file": dict(sorted(capture_parse_errors.items())),
        },
        "issue_3_observed_funnel": policy_report["observed_audit_funnel"],
        "issue_3_policy_replay": {
            "old_policy": policy_report["old_policy"],
            "new_policy": policy_report["new_policy"],
            "comparison": policy_report["comparison"],
        },
        "data_completeness": completeness,
        "manifest": manifest,
        "llm_replay": {
            "mode": "recorded_outputs_only",
            "diagnostic_only": True,
            "current_model_profitability_proof": False,
            "model_calls": 0,
        },
        "model_calls": 0,
        "historical_orders_created_by_replay": 0,
        "recorded_historical_orders_observed": len(order_times),
        "live_broker_write_calls": 0,
        "live_order_tools_called": False,
        "historical_performance_available": False,
        "historical_performance_reason": (
            "This replay validates recorded natural funnel lineage. Profitability "
            "requires a separate leakage-safe walk-forward dataset."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run read-only natural strict allocator historical replay"
    )
    parser.add_argument("--root", default=".")
    parser.add_argument("--hours", type=int, default=48)
    parser.add_argument("--asof")
    parser.add_argument("--source-revision")
    parser.add_argument("--project-root")
    parser.add_argument("--output")
    args = parser.parse_args()
    report = run_natural_strict_replay(
        args.root,
        hours=args.hours,
        asof=args.asof,
        source_revision=args.source_revision,
        project_root=args.project_root,
    )
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        output = Path(args.output).resolve()
        protected = {
            (Path(args.root).resolve() / "state").resolve(),
            (Path(args.root).resolve() / "logs").resolve(),
        }
        if any(output == path or path in output.parents for path in protected):
            raise ValueError("historical replay output cannot be written under state/ or logs/")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()
