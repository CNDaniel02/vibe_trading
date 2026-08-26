from __future__ import annotations

import argparse
from collections import Counter
from datetime import timedelta
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from scripts.core.models import parse_ts
from scripts.strategies.allocator_policy import normalize_allocator_challenge


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def _record_time(record: dict[str, Any]):
    fill = record.get("fill") if isinstance(record.get("fill"), dict) else {}
    for raw in (
        record.get("ts"),
        record.get("decision_time"),
        record.get("asof"),
        fill.get("filled_at"),
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
        if (observed := _record_time(record)) is not None and cutoff <= observed <= asof
    ]


def _verify_snapshot(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    expected = str(value.get("snapshot_hash") or "")
    unsigned = dict(value)
    unsigned.pop("snapshot_hash", None)
    serialized = json.dumps(unsigned, separators=(",", ":"), sort_keys=True)
    actual = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    if not expected or actual != expected or expected[:12] not in path.name:
        raise ValueError(f"snapshot hash mismatch: {path}")
    return value


def _snapshot_ticker(snapshot: dict[str, Any]) -> str:
    payload = snapshot.get("payload") if isinstance(snapshot.get("payload"), dict) else {}
    candidate = payload.get("candidate") if isinstance(payload.get("candidate"), dict) else {}
    return str(candidate.get("ticker") or payload.get("ticker") or "").upper()


def _snapshot_path_key(path: str | Path) -> str:
    return str(Path(path).resolve()).lower()


def _point_in_time_violations(
    snapshot: dict[str, Any],
    *,
    path: Path,
) -> list[dict[str, str]]:
    cutoff = parse_ts(str(snapshot["decision_time"]))
    payload = snapshot.get("payload") if isinstance(snapshot.get("payload"), dict) else {}
    candidate = payload.get("candidate") if isinstance(payload.get("candidate"), dict) else {}
    context = candidate.get("market_context") if isinstance(candidate.get("market_context"), dict) else {}
    agent_snapshot = payload.get("agent_snapshot") if isinstance(payload.get("agent_snapshot"), dict) else {}
    agent_market = agent_snapshot.get("market_data") if isinstance(agent_snapshot.get("market_data"), dict) else {}
    observations: list[tuple[str, Any]] = [
        ("candidate.quote.asof", (context.get("quote") or {}).get("asof")),
        ("agent_snapshot.quote.asof", (agent_market.get("quote") or {}).get("asof")),
    ]
    event_lists = [
        payload.get("events") or [],
        payload.get("new_events") or [],
        agent_snapshot.get("available_news") or [],
    ]
    for events in event_lists:
        for event in events:
            if not isinstance(event, dict):
                continue
            for field in ("published_at", "first_seen_at", "retrieved_at"):
                observations.append((f"news.{field}", event.get(field)))
    source_lists = [
        payload.get("source_metadata") or [],
        agent_snapshot.get("source_metadata") or [],
    ]
    for sources in source_lists:
        for source in sources:
            if isinstance(source, dict):
                observations.append(("source.retrieved_at", source.get("retrieved_at")))
    violations: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for field, raw in observations:
        if not raw:
            continue
        semantic_field = "quote.asof" if field.endswith("quote.asof") else field
        occurrence = (semantic_field, str(raw))
        if occurrence in seen:
            continue
        seen.add(occurrence)
        try:
            observed = parse_ts(str(raw))
        except (TypeError, ValueError):
            violations.append(
                {"path": str(path), "field": field, "observed_at": str(raw), "reason": "invalid_timestamp"}
            )
            continue
        if observed > cutoff:
            violations.append(
                {
                    "path": str(path),
                    "field": field,
                    "observed_at": observed.isoformat(),
                    "reason": "observation_after_snapshot_cutoff",
                }
            )
    return violations


def _estimated_avoidable_rank_only_cooldowns(
    cycles: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    snapshots: list[dict[str, Any]],
) -> int:
    referenced = {
        _snapshot_path_key(reference["path"])
        for decision in decisions
        if isinstance((reference := decision.get("evidence_snapshot")), dict)
        and reference.get("path")
    }
    by_ticker: dict[str, list[tuple[Any, str]]] = {}
    for item in snapshots:
        ticker = str(item.get("ticker") or "")
        if not ticker:
            continue
        by_ticker.setdefault(ticker, []).append(
            (parse_ts(str(item["decision_time"])), str(item["path"]))
        )
    for values in by_ticker.values():
        values.sort()

    recovered = 0
    for cycle in cycles:
        cycle_time = _record_time(cycle)
        if cycle_time is None:
            continue
        for skipped in cycle.get("skipped") or []:
            if not isinstance(skipped, dict) or "cooldown" not in str(
                skipped.get("reason") or ""
            ).lower():
                continue
            ticker = str(skipped.get("ticker") or "").upper()
            prior = [item for item in by_ticker.get(ticker, []) if item[0] <= cycle_time]
            if len(prior) < 2:
                continue
            previous_time, previous_path = prior[-2]
            if (
                cycle_time - previous_time <= timedelta(hours=24)
                and _snapshot_path_key(previous_path) not in referenced
            ):
                recovered += 1
    return recovered


def _record_snapshot_path(record: dict[str, Any]) -> str | None:
    reference = record.get("evidence_snapshot")
    if not isinstance(reference, dict) or not reference.get("path"):
        return None
    return _snapshot_path_key(str(reference["path"]))


def _as_candidate_records(cycle: dict[str, Any]) -> list[dict[str, Any]]:
    funnel = cycle.get("funnel")
    if not isinstance(funnel, dict):
        return []
    records = funnel.get("candidate_records")
    if not isinstance(records, list):
        return []
    return [record for record in records if isinstance(record, dict)]


def _observed_funnel(
    cycles: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    allocations: list[dict[str, Any]],
    fills: list[dict[str, Any]],
    discovery_snapshot_paths: set[str],
) -> dict[str, int]:
    candidate_records = [
        record for cycle in cycles for record in _as_candidate_records(cycle)
    ]
    linked_candidate_paths = {
        path
        for record in candidate_records
        if (path := _record_snapshot_path(record)) is not None
        and path in discovery_snapshot_paths
    }
    legacy_cycles = [cycle for cycle in cycles if not _as_candidate_records(cycle)]
    legacy_cooldown_rejected = sum(
        "cooldown" in str(item.get("reason") or "").lower()
        for cycle in legacy_cycles
        for item in (cycle.get("skipped") or [])
        if isinstance(item, dict)
    )
    legacy_candidate_count = max(
        0,
        len(discovery_snapshot_paths) - len(linked_candidate_paths),
    )
    candidates = len(discovery_snapshot_paths)
    ranking_input = sum(
        bool(record.get("ranking_entered")) for record in candidate_records
    ) + max(0, legacy_candidate_count - legacy_cooldown_rejected)
    deep_research = sum(
        bool(record.get("deep_research")) for record in candidate_records
    ) + sum(
        (path := _record_snapshot_path(decision)) is not None
        and path in discovery_snapshot_paths
        and path not in linked_candidate_paths
        for decision in decisions
    )
    proposals = sum(
        (decision.get("signal") or {}).get("action") == "propose_trade"
        for decision in decisions
    )
    watch = sum(
        (decision.get("signal") or {}).get("action") == "watch"
        for decision in decisions
    )
    return {
        "candidates": candidates,
        "ranking_input": ranking_input,
        "deep_research": deep_research,
        "structured_decisions": len(decisions),
        "watch": watch,
        "proposals": proposals,
        "allocations": len(allocations),
        "selected_instruments": sum(
            allocation.get("status") == "selected" for allocation in allocations
        ),
        "paper_orders": sum(
            int(cycle.get("paper_orders_created") or 0) for cycle in cycles
        ),
        "paper_fills": len(fills),
    }


def _blockers(
    cycles: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    allocations: list[dict[str, Any]],
) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for cycle in cycles:
        for skipped in cycle.get("skipped") or []:
            if not isinstance(skipped, dict):
                continue
            reason = str(skipped.get("reason") or "").lower()
            if "cooldown" in reason:
                counts["cooldown"] += 1
    for decision in decisions:
        challenge = dict(decision.get("challenge") or {})
        if challenge:
            normalized = normalize_allocator_challenge(challenge, legacy_fail_closed=True)
            if normalized["hard_veto"]:
                counts["hard_veto"] += 1
            elif normalized["soft_concerns"] or normalized.get("recommendation") == "reduce_confidence":
                counts["soft_concern"] += 1
        signal = dict(decision.get("signal") or {})
        if signal.get("action") == "no_trade" and not challenge.get("veto_recommended"):
            counts["model_no_trade"] += 1
    for allocation in allocations:
        reason = str(allocation.get("reason") or "").lower()
        diagnostics = allocation.get("option_candidate_diagnostics")
        if "direction" in reason:
            counts["direction_gate"] += 1
        elif "remaining move" in reason:
            counts["remaining_move"] += 1
        elif "afford" in reason or "premium" in reason or "budget" in reason:
            counts["option_affordability"] += 1
        elif "spread" in reason or "liquidity" in reason or "stale" in reason:
            counts["spread_liquidity"] += 1
        elif "risk" in reason or "position" in reason or "daily" in reason:
            counts["risk_gate"] += 1
        if isinstance(diagnostics, dict):
            rejections = diagnostics.get("rejections") or diagnostics.get("candidates") or []
            rejection_rows = (
                rejections.items() if isinstance(rejections, dict) else ((item, 1) for item in rejections)
            )
            for item, raw_count in rejection_rows:
                detail = str(item.get("reason") if isinstance(item, dict) else item).lower()
                count = max(0, int(raw_count or 0)) if isinstance(raw_count, (int, float)) else 1
                if "spread" in detail or "liquidity" in detail or "stale" in detail:
                    counts["spread_liquidity"] += count
                elif "premium" in detail or "afford" in detail or "budget" in detail:
                    counts["option_affordability"] += count
    return dict(counts)


def run_allocator_policy_replay(
    root: str | Path,
    *,
    hours: int = 48,
    asof: str | None = None,
) -> dict[str, Any]:
    """Compare policy semantics without invoking models, brokers, or state writes."""
    root_path = Path(root)
    namespace = "ai_instrument_allocator_v1"
    log_dir = root_path / "logs" / "strategy_sleeves" / namespace
    cycles_all = _read_jsonl(log_dir / "cycles.jsonl")
    decisions_all = _read_jsonl(log_dir / "decisions.jsonl")
    allocations_all = _read_jsonl(log_dir / "allocations.jsonl")
    fills_all = [
        *_read_jsonl(log_dir / "paper_fills.jsonl"),
        *_read_jsonl(log_dir / "paper_option_fills.jsonl"),
    ]
    all_snapshot_rows: list[dict[str, Any]] = []
    all_point_in_time_violations: list[dict[str, str]] = []
    for path in sorted(
        (root_path / "logs" / "ai_instrument_allocator_snapshots").glob("*.json")
    ):
        value = _verify_snapshot(path)
        snapshot_violations = _point_in_time_violations(value, path=path)
        all_point_in_time_violations.extend(snapshot_violations)
        decision_time = parse_ts(str(value["decision_time"]))
        all_snapshot_rows.append(
            {
                "path": str(path),
                "decision_time": decision_time.isoformat(),
                "snapshot_type": str(value.get("snapshot_type") or ""),
                "ticker": _snapshot_ticker(value),
                "point_in_time_valid": not snapshot_violations,
            }
        )
    timestamped = [
        observed
        for record in [*cycles_all, *decisions_all, *allocations_all, *fills_all]
        if (observed := _record_time(record)) is not None
    ]
    timestamped.extend(
        parse_ts(str(snapshot["decision_time"])) for snapshot in all_snapshot_rows
    )
    if asof is not None:
        current = parse_ts(asof)
    elif timestamped:
        current = max(timestamped)
    else:
        raise ValueError("allocator replay has no timestamped records")
    cutoff = current - timedelta(hours=max(1, int(hours)))
    cycles = _recent(cycles_all, cutoff=cutoff, asof=current)
    decisions = _recent(decisions_all, cutoff=cutoff, asof=current)
    allocations = _recent(allocations_all, cutoff=cutoff, asof=current)
    fills = _recent(fills_all, cutoff=cutoff, asof=current)

    snapshot_rows = [
        item
        for item in all_snapshot_rows
        if cutoff <= parse_ts(str(item["decision_time"])) <= current
    ]
    window_snapshot_paths = {_snapshot_path_key(item["path"]) for item in snapshot_rows}
    point_in_time_violations = [
        item
        for item in all_point_in_time_violations
        if _snapshot_path_key(item["path"]) in window_snapshot_paths
    ]

    discovery_snapshots = [
        item
        for item in snapshot_rows
        if "premarket" not in item["snapshot_type"]
        and "preopen" not in item["snapshot_type"]
    ]
    observed_audit_funnel = _observed_funnel(
        cycles,
        decisions,
        allocations,
        fills,
        {_snapshot_path_key(item["path"]) for item in discovery_snapshots},
    )
    replayable_snapshot_paths = {
        _snapshot_path_key(item["path"])
        for item in snapshot_rows
        if item["point_in_time_valid"]
    }
    excluded_snapshot_paths = window_snapshot_paths - replayable_snapshot_paths
    replayable_discovery_paths = {
        _snapshot_path_key(item["path"])
        for item in discovery_snapshots
        if item["point_in_time_valid"]
    }
    replayable_decisions = [
        decision
        for decision in decisions
        if (path := _record_snapshot_path(decision)) is not None
        and path in replayable_snapshot_paths
    ]

    candidate_records = [
        record
        for cycle in cycles
        for record in (_as_candidate_records(cycle))
    ]
    linked_candidate_paths = [
        path
        for record in candidate_records
        if (path := _record_snapshot_path(record)) is not None
        and path in window_snapshot_paths
    ]
    candidate_linkage_complete = bool(discovery_snapshots) and (
        len(linked_candidate_paths) == len(discovery_snapshots)
        and set(linked_candidate_paths)
        == {_snapshot_path_key(item["path"]) for item in discovery_snapshots}
    )
    if candidate_linkage_complete:
        replayable_candidate_records = [
            record
            for record in candidate_records
            if _record_snapshot_path(record) in replayable_discovery_paths
        ]
        strict_candidates = len(replayable_candidate_records)
        strict_ranking_input = sum(
            bool(record.get("ranking_entered"))
            for record in replayable_candidate_records
        )
        strict_deep_research = sum(
            bool(record.get("deep_research"))
            for record in replayable_candidate_records
        )
    else:
        strict_candidates = len(replayable_discovery_paths)
        strict_ranking_input = len(replayable_decisions)
        strict_deep_research = len(replayable_decisions)

    plan_snapshot_paths: dict[str, str] = {}
    for cycle in cycles:
        for plan in cycle.get("plans") or []:
            if not isinstance(plan, dict):
                continue
            plan_id = str(plan.get("plan_id") or "")
            path = _record_snapshot_path(plan)
            if plan_id and path:
                plan_snapshot_paths[plan_id] = path
    replayable_plan_ids = {
        plan_id
        for plan_id, path in plan_snapshot_paths.items()
        if path in replayable_snapshot_paths
    }
    replayable_allocations = [
        allocation
        for allocation in allocations
        if str(allocation.get("plan_id") or "") in replayable_plan_ids
    ]
    replayable_order_ids: set[str] = set()
    strict_order_count = 0
    for cycle in cycles:
        for execution in cycle.get("executions") or []:
            if not isinstance(execution, dict):
                continue
            allocation = execution.get("allocation")
            order = execution.get("order")
            if not isinstance(allocation, dict) or not isinstance(order, dict):
                continue
            if str(allocation.get("plan_id") or "") not in replayable_plan_ids:
                continue
            strict_order_count += 1
            if order.get("order_id"):
                replayable_order_ids.add(str(order["order_id"]))
    replayable_fills = [
        fill_record
        for fill_record in fills
        if str(
            (
                fill_record.get("fill")
                if isinstance(fill_record.get("fill"), dict)
                else fill_record
            ).get("order_id")
            or ""
        )
        in replayable_order_ids
    ]

    structured_count = len(replayable_decisions)
    old_proposals = sum(
        (decision.get("signal") or {}).get("action") == "propose_trade"
        for decision in replayable_decisions
    )
    new_watch = sum(
        (decision.get("signal") or {}).get("action") == "watch"
        for decision in replayable_decisions
    )
    new_proposals = old_proposals
    legacy_ambiguous = sum(
        bool((decision.get("challenge") or {}).get("veto_recommended"))
        and "hard_veto_reasons" not in (decision.get("challenge") or {})
        for decision in decisions
    )
    estimated_avoidable = _estimated_avoidable_rank_only_cooldowns(
        cycles,
        decisions,
        snapshot_rows,
    )
    selected = sum(
        allocation.get("status") == "selected"
        for allocation in replayable_allocations
    )

    old_policy = {
        "candidates": strict_candidates,
        "ranking_input": strict_ranking_input,
        "deep_research": strict_deep_research,
        "structured_decisions": structured_count,
        "watch": 0,
        "proposals": old_proposals,
        "allocations": len(replayable_allocations),
        "selected_instruments": selected,
        "paper_orders": strict_order_count,
        "paper_fills": len(replayable_fills),
    }
    new_policy = {
        **old_policy,
        "watch": new_watch,
        "proposals": new_proposals,
    }
    proposal_delta = new_proposals - old_proposals
    if snapshot_rows and not replayable_snapshot_paths:
        explanation = (
            "All snapshots in the window were excluded by point-in-time validation; "
            "observed audit counts are not treated as strict replay results."
        )
    elif proposal_delta == 0:
        explanation = (
            "Strict replay does not synthesize missing model decisions or promote "
            "legacy ambiguous vetoes; proposal recall is unchanged on the replayable subset."
        )
    else:
        explanation = (
            "Recorded time-valid structured outcomes increase proposal recall under the new policy."
        )
    return {
        "strategy": namespace,
        "window_hours": max(1, int(hours)),
        "window_started_at": cutoff.isoformat(),
        "asof": current.isoformat(),
        "old_policy": old_policy,
        "new_policy": new_policy,
        "observed_audit_funnel": observed_audit_funnel,
        "comparison": {
            "proposal_delta": proposal_delta,
            "watch_delta": new_watch,
            "estimated_avoidable_rank_only_cooldowns": estimated_avoidable,
            "candidate_linkage_complete": candidate_linkage_complete,
            "legacy_ambiguous_veto_count": legacy_ambiguous,
            "explanation": explanation,
        },
        "blockers": _blockers(cycles, decisions, allocations),
        "snapshot_integrity": {
            "checked": len(snapshot_rows),
            "valid": len(snapshot_rows),
            "invalid": 0,
        },
        "point_in_time": {
            "violation_count": len(point_in_time_violations),
            "replayable_snapshot_count": len(replayable_snapshot_paths),
            "excluded_snapshot_count": len(excluded_snapshot_paths),
            "excluded_decision_count": len(decisions) - len(replayable_decisions),
            "excluded_allocation_count": len(allocations)
            - len(replayable_allocations),
            "by_field": dict(Counter(item["field"] for item in point_in_time_violations)),
            "examples": point_in_time_violations[:10],
        },
        "model_calls": 0,
        "historical_orders_created": 0,
        "live_order_tools_called": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay allocator policy semantics read-only")
    parser.add_argument("--root", default=".")
    parser.add_argument("--hours", type=int, default=48)
    parser.add_argument("--asof")
    parser.add_argument("--output")
    args = parser.parse_args()
    report = run_allocator_policy_replay(args.root, hours=args.hours, asof=args.asof)
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()
