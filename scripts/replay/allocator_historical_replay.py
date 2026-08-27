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
    direct = str(candidate.get("ticker") or payload.get("ticker") or "").upper()
    if direct:
        return direct
    events = payload.get("events") if isinstance(payload.get("events"), list) else []
    event_tickers = {
        str(event.get("ticker") or "").upper()
        for event in events
        if isinstance(event, dict) and event.get("ticker")
    }
    return next(iter(event_tickers)) if len(event_tickers) == 1 else ""


def _missing_observation_timestamps(snapshot: dict[str, Any]) -> list[str]:
    """Return required point-in-time fields that are absent from recorded evidence."""
    payload = snapshot.get("payload") if isinstance(snapshot.get("payload"), dict) else {}
    candidate = payload.get("candidate") if isinstance(payload.get("candidate"), dict) else {}
    context = candidate.get("market_context") if isinstance(candidate.get("market_context"), dict) else {}
    agent = payload.get("agent_snapshot") if isinstance(payload.get("agent_snapshot"), dict) else {}
    market = agent.get("market_data") if isinstance(agent.get("market_data"), dict) else {}
    missing: list[str] = []

    quote = context.get("quote") if isinstance(context.get("quote"), dict) else market.get("quote")
    if isinstance(quote, dict) and not quote.get("asof"):
        missing.append("market_data.quote.asof")
    ohlcv = context.get("ohlcv") if isinstance(context.get("ohlcv"), dict) else market.get("ohlcv")
    if isinstance(ohlcv, dict) and not ohlcv.get("timestamp"):
        missing.append("market_data.ohlcv.timestamp")

    raw_chains = context.get("option_chain") or market.get("option_chain") or []
    chains = raw_chains if isinstance(raw_chains, list) else [raw_chains]
    for chain_index, chain in enumerate(chains):
        if not isinstance(chain, dict):
            continue
        if "contracts" in chain and not chain.get("asof"):
            missing.append(f"market_data.option_chain[{chain_index}].asof")
        contracts = chain.get("contracts") if "contracts" in chain else [chain]
        if not isinstance(contracts, list):
            continue
        for contract_index, contract in enumerate(contracts):
            if isinstance(contract, dict) and not contract.get("updated_at"):
                missing.append(
                    "market_data.option_chain"
                    f"[{chain_index}].contracts[{contract_index}].updated_at"
                )

    events = payload.get("events") if isinstance(payload.get("events"), list) else []
    agent_news = agent.get("available_news") if isinstance(agent.get("available_news"), list) else []
    for event_index, event in enumerate([*events, *agent_news]):
        if not isinstance(event, dict):
            continue
        for field in ("published_at", "first_seen_at", "retrieved_at"):
            if not event.get(field):
                missing.append(f"available_news[{event_index}].{field}")
    return sorted(set(missing))


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
    require_cutoff: bool = False,
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
    record_ticker = str(record.get("ticker") or "").upper()
    if record_ticker and record_ticker != str(snapshot_row.get("ticker") or ""):
        rejections["lineage: snapshot_ticker_mismatch"] += 1
        return None
    record_cutoff = record.get("data_cutoff_time")
    snapshot_cutoff = snapshot_row.get("resolved_cutoff")
    if require_cutoff and not record_cutoff:
        rejections["lineage: missing_record_cutoff"] += 1
        return None
    if require_cutoff and not snapshot_cutoff:
        rejections["lineage: missing_snapshot_cutoff"] += 1
        return None
    if record_cutoff and snapshot_cutoff:
        try:
            same_cutoff = parse_ts(str(record_cutoff)) == parse_ts(str(snapshot_cutoff))
        except (TypeError, ValueError):
            same_cutoff = False
        if not same_cutoff:
            rejections["lineage: snapshot_cutoff_mismatch"] += 1
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
    window_hours = max(1, int(hours))
    window_start = current - timedelta(hours=window_hours)
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
    cycles = _recent(cycles_all, cutoff=window_start, asof=current)
    decisions = _recent(decisions_all, cutoff=window_start, asof=current)
    allocations = _recent(allocations_all, cutoff=window_start, asof=current)
    fills = _recent(fills_all, cutoff=window_start, asof=current)

    snapshot_root = root_path / "logs" / "ai_instrument_allocator_snapshots"
    linked_cutoffs: dict[tuple[str, str], set[str]] = {}
    for decision in decisions:
        reference = decision.get("evidence_snapshot")
        raw_cutoff = decision.get("data_cutoff_time")
        if not isinstance(reference, dict) or not raw_cutoff:
            continue
        raw_path = reference.get("path")
        expected_hash = str(reference.get("snapshot_hash") or "")
        if not raw_path or not expected_hash:
            continue
        resolved = Path(str(raw_path)).resolve()
        if resolved != snapshot_root.resolve() and snapshot_root.resolve() not in resolved.parents:
            continue
        try:
            cutoff = parse_ts(str(raw_cutoff))
        except (TypeError, ValueError):
            continue
        if window_start <= cutoff <= current:
            linked_cutoffs.setdefault((_path_key(resolved), expected_hash), set()).add(
                cutoff.isoformat()
            )

    snapshot_rows: list[dict[str, Any]] = []
    invalid_snapshot_hashes: list[dict[str, str]] = []
    invalid_snapshot_envelopes: list[dict[str, str]] = []
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
            invalid_snapshot_hashes.append(
                {"path": str(path.resolve()), "error": str(exc)}
            )
            continue
        try:
            decision_time = parse_ts(str(snapshot["decision_time"]))
            ticker = _snapshot_ticker(snapshot)
            if not ticker:
                raise ValueError("immutable snapshot is missing ticker")
        except (KeyError, TypeError, ValueError) as exc:
            invalid_snapshot_envelopes.append(
                {"path": str(path.resolve()), "error": str(exc)}
            )
            continue
        snapshot_rows.append(
            {
                "path": str(path.resolve()),
                "path_key": _path_key(path),
                "snapshot": snapshot,
                "decision_time": decision_time,
                "ticker": ticker,
            }
        )
    snapshots = [
        item
        for item in snapshot_rows
        if window_start <= item["decision_time"] <= current
    ]

    rejections: Counter[str] = Counter()
    for name, count in capture_parse_errors.items():
        rejections[f"input_parse_error: {name}"] += count
    source_violations: list[dict[str, Any]] = []
    cutoff_sources: Counter[str] = Counter()
    invalid_time_snapshot_paths: set[str] = set()
    admissible_paths: set[str] = set()
    equity_rows: list[dict[str, Any]] = []
    option_chains: list[dict[str, Any]] = []
    for item in snapshots:
        snapshot = item["snapshot"]
        validation_snapshot = snapshot
        cutoff_source = "snapshot"
        if not snapshot.get("data_cutoff_time"):
            cutoff_source = "missing"
            exact_key = (
                item["path_key"],
                str(snapshot.get("snapshot_hash") or ""),
            )
            linked = linked_cutoffs.get(exact_key, set())
            if len(linked) == 1:
                validation_snapshot = {**snapshot, "data_cutoff_time": next(iter(linked))}
                cutoff_source = "linked_decision"
            elif len(linked) > 1:
                validation_snapshot = {**snapshot, "data_cutoff_time": None}
                cutoff_source = "ambiguous_linked_decision"
        validation = validate_point_in_time_snapshot(
            validation_snapshot,
            replay_asof=current.isoformat(),
        )
        cutoff_sources[cutoff_source] += 1
        item["resolved_cutoff"] = validation.get("cutoff")
        item["cutoff_source"] = cutoff_source
        for field in _missing_observation_timestamps(snapshot):
            validation["violations"].append(
                {
                    "field": field,
                    "observed_at": None,
                    "reason": "missing_observation_timestamp",
                }
            )
        validation["violation_count"] = len(validation["violations"])
        validation["valid"] = not validation["violations"]
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

    def admissible_reference(
        record: dict[str, Any],
        *,
        require_cutoff: bool = False,
    ) -> str | None:
        key = _reference_key(
            record,
            snapshot_root=snapshot_root.resolve(),
            snapshots_by_path=snapshots_by_path,
            rejections=rejections,
            require_cutoff=require_cutoff,
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
    decision_by_id: dict[str, tuple[dict[str, Any], str]] = {}
    duplicate_decision_paths: set[str] = set()
    duplicate_decision_ids: set[str] = set()
    for record in decisions:
        key = admissible_reference(record, require_cutoff=True)
        if key is None:
            continue
        is_revalidation = bool(record.get("prior_plan_id"))
        if not is_revalidation and key not in deep_paths:
            rejections["lineage: decision_without_deep_research"] += 1
            continue
        decision_id = str(record.get("snapshot_id") or "")
        if not decision_id:
            rejections["lineage: incomplete_decision_identity"] += 1
            continue
        if decision_id in decision_by_id or decision_id in duplicate_decision_ids:
            prior = decision_by_id.pop(decision_id, None)
            if prior is not None:
                decision_by_path.pop(prior[1], None)
            duplicate_decision_ids.add(decision_id)
            rejections["lineage: duplicate_decision_id"] += 1
            continue
        if (
            not is_revalidation
            and (key in decision_by_path or key in duplicate_decision_paths)
        ):
            prior = decision_by_path.pop(key, None)
            if prior is not None:
                decision_by_id.pop(str(prior.get("snapshot_id") or ""), None)
            duplicate_decision_paths.add(key)
            rejections["lineage: duplicate_decision_for_snapshot"] += 1
            continue
        decision_by_id[decision_id] = (record, key)
        if not is_revalidation:
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

    proposal_decisions = {
        decision_id: (decision, key)
        for decision_id, (decision, key) in decision_by_id.items()
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
    plan_times: dict[str, Any] = {}
    duplicate_plan_ids: set[str] = set()
    for cycle in sorted(cycles, key=_record_time):
        for plan in cycle.get("plans") or []:
            if not isinstance(plan, dict):
                continue
            plan_id = str(plan.get("plan_id") or "")
            path = admissible_reference(plan, require_cutoff=True)
            if path is None:
                continue
            source_decision_id = str(plan.get("source_decision_id") or "")
            proposal = proposal_decisions.get(source_decision_id)
            if proposal is None:
                rejections["lineage: plan_without_source_proposal"] += 1
                continue
            source_decision, source_path = proposal
            prior_plan_id = str(source_decision.get("prior_plan_id") or "")
            if prior_plan_id and prior_plan_id not in plan_paths:
                rejections["lineage: revalidation_without_prior_plan"] += 1
                continue
            if path != source_path:
                rejections["lineage: plan_source_snapshot_mismatch"] += 1
                continue
            raw_plan_time = plan.get("decision_time") or plan.get("created_at")
            raw_decision_time = source_decision.get("decision_time")
            try:
                plan_time = parse_ts(str(raw_plan_time))
                source_time = parse_ts(str(raw_decision_time))
            except (TypeError, ValueError):
                rejections["lineage: invalid_plan_timestamp"] += 1
                continue
            if plan_time < source_time or plan_time > current:
                rejections["lineage: plan_timestamp_out_of_sequence"] += 1
                continue
            if plan_id and path:
                prior_time = plan_times.get(plan_id)
                if prior_time is not None and plan_time < prior_time:
                    rejections["lineage: plan_version_timestamp_regression"] += 1
                    continue
                if (
                    prior_time is not None
                    and plan_time == prior_time
                    and plan_paths.get(plan_id) != path
                ):
                    plan_paths.pop(plan_id, None)
                    plan_times.pop(plan_id, None)
                    duplicate_plan_ids.add(plan_id)
                    rejections["lineage: duplicate_plan_id_collision"] += 1
                    continue
                if plan_id in duplicate_plan_ids:
                    continue
                plan_paths[plan_id] = path
                plan_times[plan_id] = plan_time
            else:
                rejections["lineage: incomplete_plan_identity"] += 1
    admissible_plan_ids = {
        plan_id for plan_id, path in plan_paths.items() if path in admissible_paths
    }

    admissible_allocations: list[dict[str, Any]] = []
    for allocation in allocations:
        plan_id = str(allocation.get("plan_id") or "")
        if plan_id not in admissible_plan_ids:
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
        try:
            allocation_time = parse_ts(
                str(
                    allocation.get("data_cutoff_time")
                    or allocation.get("decision_time")
                )
            )
        except (TypeError, ValueError):
            rejections["lineage: invalid_allocation_timestamp"] += 1
            continue
        if allocation_time < plan_times[plan_id] or allocation_time > current:
            rejections["lineage: allocation_timestamp_out_of_sequence"] += 1
            continue
        admissible_allocations.append(allocation)
        if allocation.get("status") != "selected":
            reason = str(allocation.get("reason") or "unspecified")
            rejections[f"allocation: {reason}"] += 1

    allocation_id_counts = Counter(
        str(item.get("allocation_id") or "") for item in admissible_allocations
    )
    duplicate_allocation_ids = {
        allocation_id
        for allocation_id, count in allocation_id_counts.items()
        if allocation_id and count > 1
    }
    if duplicate_allocation_ids:
        rejections["lineage: duplicate_allocation_id"] += sum(
            allocation_id_counts[allocation_id]
            for allocation_id in duplicate_allocation_ids
        )
    missing_allocation_ids = sum(
        not str(item.get("allocation_id") or "") for item in admissible_allocations
    )
    if missing_allocation_ids:
        rejections["lineage: incomplete_allocation_identity"] += missing_allocation_ids
    admissible_allocations = [
        item
        for item in admissible_allocations
        if str(item.get("allocation_id") or "")
        and str(item.get("allocation_id")) not in duplicate_allocation_ids
    ]

    selected_allocations = {
        str(allocation.get("allocation_id") or ""): allocation
        for allocation in admissible_allocations
        if allocation.get("status") == "selected" and allocation.get("allocation_id")
    }
    order_times: dict[str, Any] = {}
    order_instruments: dict[str, dict[str, Any]] = {}
    duplicate_order_ids: set[str] = set()
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
            if str(allocation.get("plan_id") or "") != str(
                admitted_allocation.get("plan_id") or ""
            ):
                rejections["lineage: execution_plan_mismatch"] += 1
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
                source_violations.extend(
                    {"order_id": order.get("order_id"), **violation}
                    for violation in order_validation["violations"]
                )
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
            if order_id in order_times or order_id in duplicate_order_ids:
                order_times.pop(order_id, None)
                order_instruments.pop(order_id, None)
                duplicate_order_ids.add(order_id)
                rejections["lineage: duplicate_order_id"] += 1
                continue
            order_times[order_id] = created_at
            order_instruments[order_id] = dict(
                admitted_allocation.get("selected_instrument") or {}
            )

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
        expected_instrument = order_instruments.get(order_id, {})
        instrument_type = str(expected_instrument.get("instrument_type") or "")
        if instrument_type == "equity" and str(fill.get("symbol") or "").upper() != str(
            expected_instrument.get("ticker") or ""
        ).upper():
            rejections["lineage: fill_instrument_mismatch"] += 1
            continue
        if instrument_type in {"call", "put"} and str(
            fill.get("option_id") or ""
        ) != str(expected_instrument.get("option_id") or ""):
            rejections["lineage: fill_instrument_mismatch"] += 1
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

    try:
        policy_report = run_allocator_policy_replay(
            root_path,
            hours=window_hours,
            asof=current.isoformat(),
        )
        policy_replay_error = None
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        policy_report = {
            "observed_audit_funnel": {},
            "old_policy": {},
            "new_policy": {},
            "comparison": {},
            "blockers": [],
            "snapshot_integrity": {},
            "point_in_time": {},
            "historical_orders_created": 0,
            "live_order_tools_called": False,
        }
        policy_replay_error = f"{type(exc).__name__}: {exc}"
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
            "source_violation_count": len(source_violations),
            "source_snapshot_violation_count": len(invalid_time_snapshot_paths),
            "admitted_violation_count": 0,
            "excluded_snapshot_count": len(snapshots) - len(admissible_paths),
            "by_field": dict(
                Counter(str(item.get("field")) for item in source_violations)
            ),
            "examples": source_violations[:10],
            "cutoff_sources": dict(sorted(cutoff_sources.items())),
        },
        "snapshot_integrity": {
            "checked": len(snapshot_paths),
            "valid_hashes": len(snapshot_paths) - len(invalid_snapshot_hashes),
            "invalid_hashes": len(invalid_snapshot_hashes),
            "valid_envelopes": len(snapshot_rows),
            "invalid_envelopes": len(invalid_snapshot_envelopes),
            "invalid_examples": [
                *invalid_snapshot_hashes,
                *invalid_snapshot_envelopes,
            ][:10],
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
            "evidence_type": "legacy_issue_3_policy_diagnostic",
            "cutoff_semantics": "legacy_issue_3_decision_time_diagnostic",
            "comparable_to_strict_funnel": False,
            "old_policy": policy_report["old_policy"],
            "new_policy": policy_report["new_policy"],
            "comparison": policy_report["comparison"],
            "blockers": policy_report.get("blockers", []),
            "snapshot_integrity": policy_report.get("snapshot_integrity", {}),
            "point_in_time": policy_report.get("point_in_time", {}),
            "historical_orders_created": policy_report.get(
                "historical_orders_created", 0
            ),
            "live_order_tools_called": policy_report.get(
                "live_order_tools_called", False
            ),
            "error": policy_replay_error,
        },
        "data_completeness": completeness,
        "manifest": manifest,
        "llm_replay": {
            "mode": "recorded_outputs_only",
            "strategy_reexecution_performed": False,
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
