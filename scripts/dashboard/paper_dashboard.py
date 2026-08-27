"""Local, read-only GUI for the paper/shadow trading audit trail.

This server never imports a broker adapter and exposes no mutation endpoint.
It is deliberately separate from the scheduler so it can inspect a live service
without competing for its process lock or current-user OAuth credentials.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import timedelta
from functools import lru_cache
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from scripts.core.config import load_runtime_config
from scripts.core.models import parse_ts, utc_now
from scripts.decision.signed_return_signal import derive_signal_summary
from scripts.runtime.process_lock import ProcessLock
from scripts.evaluation.calculate_metrics import calculate_metrics
from scripts.evaluation.evaluate_news_drift import calculate_news_drift_metrics
from scripts.replay.allocator_policy_replay import run_allocator_policy_replay
from scripts.strategies.allocator_policy import normalize_allocator_challenge
from scripts.strategies.allocator_state import AllocatorStateStore


_COMPLETED_ORDER_STATUSES = {"filled", "cancelled", "expired", "rejected"}


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _dict_values(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, dict):
        return []
    return [item for item in value.values() if isinstance(item, dict)]


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _file_signature(paths: list[Path]) -> tuple[tuple[str, int | None, int | None], ...]:
    signature: list[tuple[str, int | None, int | None]] = []
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            signature.append((str(path), None, None))
        else:
            signature.append((str(path), stat.st_mtime_ns, stat.st_size))
    return tuple(signature)


def _metrics_signature(root: Path, namespace: str | None) -> tuple[tuple[str, int | None, int | None], ...]:
    state_dir = root / "state"
    log_dir = root / "logs"
    if namespace:
        state_dir = state_dir / "strategy_sleeves" / namespace
        log_dir = log_dir / "strategy_sleeves" / namespace
    paths = [
        *(sorted((root / "config").glob("*.yaml"))),
        *(state_dir / name for name in (
            "paper_account.json",
            "paper_positions.json",
            "paper_orders.json",
            "paper_option_positions.json",
            "paper_option_orders.json",
        )),
        *(log_dir / name for name in (
            "portfolio_snapshots.jsonl",
            "paper_fills.jsonl",
            "paper_option_fills.jsonl",
            "audit.jsonl",
        )),
    ]
    if namespace == "ai_gated_technical_v1":
        paths.append(root / "logs" / "ai_gated_decisions.jsonl")
    return _file_signature(paths)


@lru_cache(maxsize=16)
def _cached_metrics(
    root_text: str,
    namespace: str | None,
    signature: tuple[tuple[str, int | None, int | None], ...],
) -> dict[str, Any]:
    del signature
    return calculate_metrics(Path(root_text), namespace=namespace)


def _safe_metrics(root: Path, namespace: str | None = None) -> dict[str, Any]:
    try:
        resolved = root.resolve()
        return _cached_metrics(
            str(resolved),
            namespace,
            _metrics_signature(resolved, namespace),
        )
    except (
        OSError,
        json.JSONDecodeError,
        AttributeError,
        TypeError,
        ValueError,
        KeyError,
    ) as exc:
        return {
            "namespace": namespace,
            "metrics_available": False,
            "error": f"metrics unavailable: {type(exc).__name__}",
        }


@lru_cache(maxsize=8)
def _cached_news_drift_metrics(
    root_text: str,
    signature: tuple[tuple[str, int | None, int | None], ...],
) -> dict[str, Any]:
    del signature
    return calculate_news_drift_metrics(Path(root_text))


def _safe_news_drift_metrics(root: Path) -> dict[str, Any]:
    try:
        resolved = root.resolve()
        signature = _file_signature(
            [
                *(sorted((resolved / "config").glob("*.yaml"))),
                resolved / "state" / "news_events.sqlite",
                resolved / "state" / "news_events.sqlite-wal",
                resolved / "state" / "news_events.sqlite-shm",
                resolved / "logs" / "llm_usage.jsonl",
                resolved / "logs" / "news_drift_cycles.jsonl",
            ]
        )
        metrics = _cached_news_drift_metrics(str(resolved), signature)
        if not isinstance(metrics, dict):
            raise TypeError("news drift metrics must be an object")
        return {"metrics_available": True, **metrics}
    except (
        OSError,
        sqlite3.Error,
        json.JSONDecodeError,
        AttributeError,
        TypeError,
        ValueError,
        KeyError,
    ) as exc:
        return {
            "strategy": "llm_news_drift_v1",
            "metrics_available": False,
            "error": f"metrics unavailable: {type(exc).__name__}",
        }


@lru_cache(maxsize=8)
def _cached_allocator_policy_replay(
    root_text: str,
    signature: tuple[tuple[str, int | None, int | None], ...],
) -> dict[str, Any]:
    del signature
    return run_allocator_policy_replay(Path(root_text), hours=48)


def _safe_allocator_policy_replay(
    root: Path,
    namespace: str,
    fallback: dict[str, Any],
) -> dict[str, Any]:
    """Recompute only when append-only allocator inputs change; never write state."""
    try:
        resolved = root.resolve()
        log_dir = resolved / "logs" / "strategy_sleeves" / namespace
        signature = _file_signature(
            [
                log_dir / "cycles.jsonl",
                log_dir / "decisions.jsonl",
                log_dir / "allocations.jsonl",
                log_dir / "paper_fills.jsonl",
                log_dir / "paper_option_fills.jsonl",
                resolved / "logs" / "ai_instrument_allocator_snapshots",
            ]
        )
        report = _cached_allocator_policy_replay(str(resolved), signature)
        if not isinstance(report, dict):
            raise TypeError("allocator policy replay must return an object")
        return report
    except (
        OSError,
        json.JSONDecodeError,
        AttributeError,
        TypeError,
        ValueError,
        KeyError,
    ):
        return fallback


def _allocator_validation_view(
    report: dict[str, Any],
    allocator_metrics: dict[str, Any] | None,
) -> dict[str, Any]:
    functional = _as_dict(report.get("functional_liveness"))
    historical = _as_dict(report.get("historical_performance"))
    forward = _as_dict(report.get("forward_evidence"))
    time_validation = _as_dict(historical.get("time_validation"))
    strict_counts = _as_dict(_as_dict(historical.get("strict_funnel")).get("counts"))
    completeness = _as_dict(historical.get("data_completeness"))
    options = _as_dict(completeness.get("options"))
    metrics = _as_dict(allocator_metrics)
    if not forward:
        closed = int(metrics.get("closed_trade_count") or 0)
        forward = {
            "evidence_type": "forward_paper_evidence",
            "available": bool(metrics),
            "closed_trade_count": closed,
            "realized_pnl_usd": metrics.get("realized_pnl"),
            "profitability_claim": (
                "forward_evidence_sufficient"
                if metrics.get("evidence_sufficient") is True
                else "insufficient_forward_evidence"
            ),
        }
    functional_status = str(functional.get("status") or "not_run")
    historical_status = (
        "performance_ready"
        if historical.get("historical_performance_available") is True
        else "diagnostic_only"
        if historical
        else "not_generated"
    )
    forward_status = str(
        forward.get("profitability_claim") or "insufficient_forward_evidence"
    )
    return {
        "report_available": bool(report),
        "generated_at": report.get("generated_at"),
        "evidence_lines": [
            {
                "key": "functional_liveness",
                "title": "功能闭环",
                "status": functional_status,
                "detail": (
                    f"固定场景通过 {int(_as_dict(functional.get('summary')).get('passed') or 0)} / "
                    f"{int(_as_dict(functional.get('summary')).get('scenario_count') or 0)}；"
                    "只证明正式路径能运行，不证明盈利。"
                ),
            },
            {
                "key": "historical_performance",
                "title": "严格历史证据",
                "status": historical_status,
                "detail": (
                    f"候选 {int(strict_counts.get('candidates') or 0)}，提案 "
                    f"{int(strict_counts.get('proposals') or 0)}，成交 "
                    f"{int(strict_counts.get('paper_fills') or 0)}；纳入未来数据违规 "
                    f"{int(time_validation.get('admitted_violation_count') or 0)}。"
                ),
            },
            {
                "key": "forward_evidence",
                "title": "真实向前模拟",
                "status": forward_status,
                "detail": (
                    f"已平仓 {int(forward.get('closed_trade_count') or 0)} 笔，"
                    f"已实现 PnL {forward.get('realized_pnl_usd')}；"
                    "这是当前模型盈利判断的唯一直接证据。"
                ),
            },
        ],
        "strict_funnel": strict_counts,
        "time_validation": time_validation,
        "data_completeness": completeness,
        "option_claim": (
            "executable_option_pnl"
            if options.get("executable_pnl_claim_allowed") is True
            else "synthetic_option_sensitivity_only"
        ),
        "functional": functional,
        "historical": historical,
        "forward": forward,
    }


def _read_jsonl(path: Path, limit: int = 400) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            position = handle.tell()
            chunks: list[bytes] = []
            newline_count = 0
            while position > 0 and newline_count < limit + 1:
                read_size = min(64 * 1024, position)
                position -= read_size
                handle.seek(position)
                chunk = handle.read(read_size)
                chunks.append(chunk)
                newline_count += chunk.count(b"\n")
            starts_on_line_boundary = position == 0
            if position > 0:
                handle.seek(position - 1)
                starts_on_line_boundary = handle.read(1) == b"\n"
    except OSError:
        return []
    lines = b"".join(reversed(chunks)).splitlines()
    if position > 0 and not starts_on_line_boundary:
        lines = lines[1:]
    records: list[dict[str, Any]] = []
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            record = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            # The service can be appending while the dashboard reads. Ignore a
            # single incomplete line and retry on the next browser refresh.
            continue
        if isinstance(record, dict):
            records.append(record)
            if len(records) == limit:
                break
    records.reverse()
    return records


def _build_trade_funnel(
    *,
    now: str,
    allocator_profile: dict[str, Any],
    audit_records: list[dict[str, Any]],
    allocator_cycles: list[dict[str, Any]],
    allocator_decisions: list[dict[str, Any]],
    allocator_fill_records: list[dict[str, Any]],
    allocator_policy_replay: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Summarize the rolling paper-entry path without changing runtime state."""
    replay = _as_dict(allocator_policy_replay)
    replay_old = _as_dict(replay.get("old_policy"))
    replay_new = _as_dict(replay.get("new_policy"))
    replay_observed = _as_dict(replay.get("observed_audit_funnel"))
    replay_comparison = _as_dict(replay.get("comparison"))
    try:
        current = parse_ts(str(replay.get("asof"))) if replay_old else parse_ts(now)
    except (TypeError, ValueError):
        current = parse_ts(now)
        replay = {}
        replay_old = {}
        replay_new = {}
        replay_observed = {}
        replay_comparison = {}
    window_hours = max(1, int(replay.get("window_hours") or 48))
    cutoff = current - timedelta(hours=window_hours)

    def recent(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        kept: list[dict[str, Any]] = []
        for record in records:
            nested_fill = _as_dict(record.get("fill"))
            raw_time = next(
                (
                    value
                    for value in (
                        record.get("ts"),
                        record.get("decision_time"),
                        record.get("asof"),
                        nested_fill.get("filled_at"),
                    )
                    if value
                ),
                None,
            )
            if raw_time is None:
                continue
            try:
                observed = parse_ts(str(raw_time))
            except (TypeError, ValueError):
                continue
            if cutoff <= observed <= current:
                kept.append(record)
        return kept

    recent_audit = recent(audit_records)
    forward_cycles = [
        record
        for record in recent_audit
        if record.get("event") == "forward_cycle_complete"
    ]
    equity_screened = sum(int(record.get("snapshots") or 0) for record in forward_cycles)
    equity_signals = sum(
        int(record.get("active_candidates") or 0) for record in forward_cycles
    )
    equity_orders = sum(
        len(record.get("orders") or []) for record in forward_cycles
    )
    option_decisions = [
        decision
        for record in forward_cycles
        for decision in (record.get("option_decisions") or [])
        if isinstance(decision, dict)
    ]
    option_signals = sum(
        decision.get("action") == "buy_to_open" for decision in option_decisions
    )
    option_orders = sum(
        len(record.get("option_entries") or []) for record in forward_cycles
    )
    equity_executable = any(
        record.get("active_execution")
        and record.get("active_execution") != "shadow_only"
        for record in forward_cycles
    )
    option_executable = any(
        decision.get("action") == "buy_to_open"
        and decision.get("execution_status") != "entry_frozen"
        for decision in option_decisions
    )

    cycles = recent(allocator_cycles)
    decisions = recent(allocator_decisions)
    fills = recent(allocator_fill_records)
    skipped = [
        item
        for cycle in cycles
        for item in (cycle.get("skipped") or [])
        if isinstance(item, dict)
    ]
    plans = [
        item
        for cycle in cycles
        for item in (cycle.get("plans") or [])
        if isinstance(item, dict)
    ]
    cycle_watches = [
        item
        for cycle in cycles
        for item in (cycle.get("watches") or [])
        if isinstance(item, dict)
    ]
    executions = [
        item
        for cycle in cycles
        for item in (cycle.get("executions") or [])
        if isinstance(item, dict)
    ]
    proposals = [
        record
        for record in decisions
        if _as_dict(record.get("signal")).get("action") == "propose_trade"
    ]
    watches = [
        record
        for record in decisions
        if _as_dict(record.get("signal")).get("action") == "watch"
    ]
    hard_veto_count = 0
    soft_concern_count = 0
    model_no_trade_count = 0
    for record in decisions:
        challenge = _as_dict(record.get("challenge"))
        normalized = normalize_allocator_challenge(
            challenge,
            legacy_fail_closed=True,
        )
        if normalized["hard_veto"]:
            hard_veto_count += 1
        elif normalized["soft_concerns"]:
            soft_concern_count += 1
        if (
            _as_dict(record.get("signal")).get("action") == "no_trade"
            and not normalized["hard_veto"]
        ):
            model_no_trade_count += 1

    funnel_totals: Counter[str] = Counter()
    for cycle in cycles:
        funnel = _as_dict(cycle.get("funnel"))
        for key in (
            "candidate_discovery",
            "ranking_input",
            "deep_research",
        ):
            funnel_totals[key] += int(funnel.get(key) or 0)

    minimum_mass = float(allocator_profile.get("minimum_direction_mass", 0.55))
    minimum_margin = float(
        allocator_profile.get("minimum_direction_margin", 0.15)
    )
    direction_threshold_passes = 0
    for record in proposals:
        try:
            summary = derive_signal_summary(_as_dict(record.get("signal")))
        except ValueError:
            continue
        masses = sorted(
            (
                float(summary["bearish_probability"]),
                float(summary["neutral_probability"]),
                float(summary["bullish_probability"]),
            ),
            reverse=True,
        )
        if (
            summary["direction"] != "neutral"
            and masses[0] >= minimum_mass
            and masses[0] - masses[1] >= minimum_margin
        ):
            direction_threshold_passes += 1

    blocker_counts: Counter[str] = Counter(
        {
            str(code): max(0, int(count or 0))
            for code, count in _as_dict(replay.get("blockers")).items()
        }
    )
    if not blocker_counts:
        blocker_counts.update(
            {
                "hard_veto": hard_veto_count,
                "soft_concern": soft_concern_count,
                "model_no_trade": model_no_trade_count,
            }
        )
        for item in skipped:
            reason = str(item.get("reason") or "").lower()
            if "cooldown" in reason:
                blocker_counts["cooldown"] += 1
            elif "holding period does not match horizon" in reason:
                blocker_counts["holding_period_mismatch"] += 1
            elif "unsupported evidence" in reason or "snapshot" in reason:
                blocker_counts["unsupported_evidence"] += 1
        for execution in executions:
            if execution.get("status") != "no_trade":
                continue
            reason = str(execution.get("reason") or "").lower()
            if "risk" in reason or "position" in reason or "daily" in reason:
                blocker_counts["risk_gate"] += 1
            elif "remaining move" in reason:
                blocker_counts["remaining_move"] += 1
            elif "afford" in reason or "premium" in reason or "budget" in reason:
                blocker_counts["option_affordability"] += 1
            elif "spread" in reason or "liquidity" in reason or "stale" in reason:
                blocker_counts["spread_liquidity"] += 1
            else:
                blocker_counts["execution_data"] += 1

    blocker_labels = {
        "cooldown": (
            "冷却期避免重复研究",
            "同一股票或同一事件没有新信息，系统不重复花费模型调用。",
            "info",
        ),
        "holding_period_mismatch": (
            "模型持仓期限字段不合法",
            "模型给出的 horizon 与持仓天数冲突，系统按 fail-closed 拒绝。",
            "bad",
        ),
        "unsupported_evidence": (
            "模型引用了快照外证据",
            "引用无法在当时保存的证据快照中核对，因此不能交易。",
            "warn",
        ),
        "hard_veto": (
            "Challenge 硬否决",
            "存在关键事实、时间、来源、mandate 或 horizon 冲突，必须 fail-closed。",
            "bad",
        ),
        "soft_concern": (
            "Challenge 软顾虑",
            "不确定性、部分 price-in 或次要证据不足会降置信度，但不会自动否决。",
            "warn",
        ),
        "model_no_trade": (
            "Decision 主动不交易",
            "完整研究后仍没有足够方向、催化或可证伪 thesis。",
            "info",
        ),
        "direction_gate": (
            "方向概率门槛未通过",
            "未校准 signed buckets 的单侧质量或领先幅度不足；不会据此计算概率 EV。",
            "info",
        ),
        "remaining_move": (
            "剩余可交易空间不足",
            "相对固定 reference price 的剩余 move 不足以覆盖成本与保守 hurdle。",
            "info",
        ),
        "option_affordability": (
            "期权权利金超预算",
            "合约未通过单笔 premium 风险或 $2,000 可负担性对照。",
            "warn",
        ),
        "spread_liquidity": (
            "期权价差、流动性或时效不合格",
            "报价过期或 spread 超过硬上限，确定性执行层拒绝使用。",
            "warn",
        ),
        "risk_gate": (
            "确定性风控拒绝",
            "提案未通过账户、仓位或交易规则检查。",
            "warn",
        ),
        "execution_data": (
            "执行前行情或数据失败",
            "提案存在，但执行所需的最新报价或合约数据未通过检查。",
            "bad",
        ),
    }
    blockers = [
        {
            "code": code,
            "label": blocker_labels[code][0],
            "explanation": blocker_labels[code][1],
            "kind": blocker_labels[code][2],
            "count": count,
        }
        for code, count in sorted(
            blocker_counts.items(), key=lambda item: (-item[1], item[0])
        )
        if code in blocker_labels and count > 0
    ]

    entry_fills = 0
    for record in fills:
        fill = _as_dict(record.get("fill")) or record
        if fill.get("side") == "buy" or fill.get("intent") == "buy_to_open":
            entry_fills += 1
    paper_orders = sum(int(cycle.get("paper_orders_created") or 0) for cycle in cycles)
    candidate_reviews = (
        funnel_totals["candidate_discovery"]
        or len(skipped) + len(plans) + len(cycle_watches)
    )
    ranking_inputs = funnel_totals["ranking_input"] or len(decisions)
    deep_research = funnel_totals["deep_research"] or len(decisions)
    model_decisions = len(decisions)
    watch_count = len(watches)
    trade_proposals = len(proposals)
    allocation_attempts = len(executions)
    selected_instruments = sum(
        execution.get("status") == "selected" for execution in executions
    )
    display_funnel = replay_observed or replay_old
    if display_funnel:
        candidate_reviews = int(display_funnel.get("candidates") or 0)
        ranking_inputs = int(display_funnel.get("ranking_input") or 0)
        deep_research = int(
            display_funnel.get("deep_research")
            or display_funnel.get("structured_decisions")
            or 0
        )
        model_decisions = int(display_funnel.get("structured_decisions") or 0)
        watch_count = int(display_funnel.get("watch") or 0)
        trade_proposals = int(display_funnel.get("proposals") or 0)
        allocation_attempts = int(display_funnel.get("allocations") or 0)
        selected_instruments = int(
            display_funnel.get("selected_instruments") or 0
        )
        paper_orders = int(display_funnel.get("paper_orders") or 0)
        entry_fills = int(display_funnel.get("paper_fills") or 0)
        hard_veto_count = int(blocker_counts.get("hard_veto") or 0)
        soft_concern_count = int(blocker_counts.get("soft_concern") or 0)
        model_no_trade_count = int(blocker_counts.get("model_no_trade") or 0)
    rejected_by_model = max(0, model_decisions - trade_proposals)
    if paper_orders:
        root_cause = {
            "code": "orders_created",
            "title": "漏斗已经产生模拟订单",
            "detail": f"过去 48 小时创建了 {paper_orders} 笔模拟订单。",
        }
    elif rejected_by_model >= max(1, allocation_attempts):
        root_cause = {
            "code": "allocator_proposal_bottleneck",
            "title": "多数候选停在 AI 研究与质询阶段",
            "detail": (
                f"{model_decisions} 次结构化模型决策形成 {watch_count} 个 watch、"
                f"{trade_proposals} 个交易提案；"
                "确定性执行层没有足够提案可处理。"
            ),
        }
    elif allocation_attempts:
        root_cause = {
            "code": "allocator_execution_bottleneck",
            "title": "提案停在执行前数据或风控检查",
            "detail": (
                f"已进入 allocation / 执行检查 {allocation_attempts} 次，但没有创建模拟订单；"
                "请查看下方执行前行情、合约数据和风控原因。"
            ),
        }
    elif trade_proposals:
        root_cause = {
            "code": "allocator_waiting_for_execution",
            "title": "已有提案，但尚未进入允许的执行窗口",
            "detail": f"过去 48 小时形成 {trade_proposals} 个提案，尚无执行尝试。",
        }
    else:
        root_cause = {
            "code": "allocator_no_proposal",
            "title": "没有候选形成可执行提案",
            "detail": "研究仍在运行，但所有候选均为 no-trade 或失败关闭。",
        }

    return {
        "window_hours": window_hours,
        "window_started_at": cutoff.isoformat(),
        "asof": current.isoformat(),
        "root_cause": root_cause,
        "allocator": {
            "candidate_reviews": candidate_reviews,
            "ranking_inputs": ranking_inputs,
            "deep_research": deep_research,
            "model_decisions": model_decisions,
            "watch": watch_count,
            "trade_proposals": trade_proposals,
            "hard_veto": hard_veto_count,
            "soft_concern": soft_concern_count,
            "model_no_trade": model_no_trade_count,
            "direction_threshold_passes": direction_threshold_passes,
            "allocation_attempts": allocation_attempts,
            "selected_instruments": selected_instruments,
            "execution_attempts": allocation_attempts,
            "paper_orders": paper_orders,
            "paper_fills": entry_fills,
            "minimum_direction_mass": minimum_mass,
            "minimum_direction_margin": minimum_margin,
        },
        "replay_comparison": {
            "old_policy": replay_old,
            "new_policy": replay_new,
            "observed_audit_funnel": replay_observed,
            "comparison": replay_comparison,
            "snapshot_integrity": _as_dict(replay.get("snapshot_integrity")),
            "point_in_time": _as_dict(replay.get("point_in_time")),
            "historical_orders_created": int(
                replay.get("historical_orders_created") or 0
            ),
            "live_order_tools_called": bool(
                replay.get("live_order_tools_called", False)
            ),
        }
        if replay_old
        else None,
        "baselines": {
            "equity": {
                "screened": equity_screened,
                "signals": equity_signals,
                "orders": equity_orders,
                "executable": equity_executable,
                "mode": "paper" if equity_executable else "shadow_only",
            },
            "options": {
                "screened": len(option_decisions),
                "signals": option_signals,
                "orders": option_orders,
                "executable": option_executable,
                "mode": "paper" if option_executable else "entry_frozen",
            },
        },
        "blockers": blockers,
    }


def _bounded_orders(
    values: list[dict[str, Any]],
    *,
    completed_limit: int = 20,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    rows = [value for value in values if isinstance(value, dict)]
    rows.sort(
        key=lambda item: str(item.get("submitted_at") or item.get("created_at") or ""),
        reverse=True,
    )
    open_orders = [
        item for item in rows if item.get("status") not in _COMPLETED_ORDER_STATUSES
    ]
    completed = [
        item for item in rows if item.get("status") in _COMPLETED_ORDER_STATUSES
    ]
    included = completed[:completed_limit]
    return [*open_orders, *included], {
        "open_total": len(open_orders),
        "completed_total": len(completed),
        "completed_included": len(included),
    }


def _quote_observation(
    observed_at: Any,
    *,
    now: str,
    stale_after_seconds: int,
    source: Any = None,
) -> dict[str, Any]:
    if not observed_at:
        return {
            "observed_at": None,
            "age_seconds": None,
            "stale": True,
            "source": source,
        }
    try:
        raw_age = (parse_ts(now) - parse_ts(str(observed_at))).total_seconds()
        age = max(0.0, raw_age)
    except (TypeError, ValueError):
        return {
            "observed_at": str(observed_at),
            "age_seconds": None,
            "stale": True,
            "source": source,
        }
    return {
        "observed_at": str(observed_at),
        "age_seconds": round(age, 1),
        "stale": raw_age < 0 or age > stale_after_seconds,
        "source": source,
    }


def _market_data_observations(
    decision_records: list[dict[str, Any]],
    option_diagnostics: list[dict[str, Any]],
    *,
    now: str,
    equity_stale_after_seconds: int,
    option_stale_after_seconds: int,
) -> dict[str, Any]:
    equity_quote: dict[str, Any] = {}
    for record in reversed(decision_records):
        snapshot = _as_dict(record.get("snapshot"))
        quote = _as_dict(_as_dict(snapshot.get("market_data")).get("quote"))
        if quote.get("asof"):
            equity_quote = quote
            break

    option_observed_at = None
    option_source = None
    for record in reversed(option_diagnostics):
        diagnostics = _as_dict(record.get("diagnostics"))
        if not diagnostics:
            diagnostics = _as_dict(
                _as_dict(record.get("decision")).get(
                    "contract_selection_diagnostics"
                )
            )
        option_observed_at = diagnostics.get("quotes_observed_at")
        if option_observed_at:
            option_source = diagnostics.get("source")
            break

    return {
        "equity": _quote_observation(
            equity_quote.get("asof"),
            now=now,
            stale_after_seconds=equity_stale_after_seconds,
            source=equity_quote.get("source"),
        ),
        "options": _quote_observation(
            option_observed_at,
            now=now,
            stale_after_seconds=option_stale_after_seconds,
            source=option_source,
        ),
    }


def _last(records: list[dict[str, Any]], predicate: Any) -> dict[str, Any] | None:
    for record in reversed(records):
        if predicate(record):
            return record
    return None


def _technical_reasons(decision: dict[str, Any], snapshot: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    regime = decision.get("regime", {})
    technical = decision.get("technical", {})
    if not regime.get("eligible", False):
        reasons.extend(regime.get("reasons") or ["market regime is not eligible"])
    if technical.get("reasons"):
        reasons.extend(str(value) for value in technical["reasons"])
        if technical.get("chase_allowed") is False:
            reasons.append("chase score exceeds the configured entry cap")
        return reasons
    if snapshot.get("market_session") != "regular":
        reasons.append("outside the regular NYSE session")
    if not technical.get("quote_valid", False):
        reasons.append(str(technical.get("quote_reason", "quote is not valid")))
    thresholds = technical.get("thresholds", {})
    min_rs = float(thresholds.get("min_relative_strength_20d_pct", 0.25))
    min_move_5d = float(thresholds.get("min_price_change_5d_pct", 0.5))
    min_volume = float(thresholds.get("min_volume_ratio", 0.4))
    if float(technical.get("relative_strength_20d", 0)) < min_rs:
        reasons.append(f"20-day relative strength is below {min_rs:g} percentage points")
    if float(technical.get("price_change_5d_pct", 0)) < min_move_5d:
        reasons.append(f"5-day price change is below {min_move_5d:g}")
    if technical.get("volume_ratio") is None:
        reasons.append("intraday volume confirmation is unavailable")
    elif float(technical.get("volume_ratio", 0)) < min_volume:
        reasons.append(f"volume ratio is below {min_volume:g}")
    return reasons or ["all deterministic entry conditions passed"]


def _candidate_rows(decision_records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    latest: dict[str, dict[str, Any]] = {}
    by_snapshot: dict[str, dict[str, Any]] = {}
    for record in reversed(decision_records):
        event = record.get("event")
        if event not in {"strategy_comparison", "baseline_decision"}:
            continue
        decision = record.get("active_strategy") if event == "strategy_comparison" else record.get("decision")
        baseline = record.get("baseline_shadow") if event == "strategy_comparison" else record.get("decision")
        snapshot = record.get("snapshot")
        if not isinstance(decision, dict) or not isinstance(snapshot, dict):
            continue
        ticker = str(decision.get("ticker", ""))
        snapshot_id = str(decision.get("snapshot_id", ""))
        if event == "strategy_comparison":
            reasons = decision.get("reasons", [])
            score = decision.get("score")
            minimum_score = decision.get("minimum_entry_score")
            feature_scores = decision.get("feature_scores", {})
            weight_state = decision.get("weight_state", {})
            hard_gate_passed = decision.get("hard_gate_passed")
        else:
            reasons = _technical_reasons(decision, snapshot)
            score = None
            minimum_score = None
            feature_scores = {}
            weight_state = {}
            hard_gate_passed = None
        item = {
            "asof": record.get("ts"),
            "ticker": ticker,
            "action": decision.get("action", "no_trade"),
            "technical": decision.get("technical", {}),
            "score": score,
            "minimum_entry_score": minimum_score,
            "feature_scores": feature_scores,
            "weight_state": weight_state,
            "hard_gate_passed": hard_gate_passed,
            "reasons": reasons,
            "baseline_shadow": baseline,
            "snapshot_id": snapshot_id,
            "binary_event_within_days": snapshot.get("market_data", {}).get(
                "binary_event_within_days"
            ),
        }
        if ticker and ticker not in latest:
            latest[ticker] = item
        if snapshot_id and snapshot_id not in by_snapshot:
            by_snapshot[snapshot_id] = item
    return sorted(latest.values(), key=lambda item: item["ticker"]), by_snapshot


def _exit_rows(decision_records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for record in reversed(decision_records):
        if record.get("event") != "exit_evaluation":
            continue
        symbol = str(record.get("symbol", ""))
        if symbol and symbol not in latest:
            latest[symbol] = {"asof": record.get("ts"), **dict(record.get("decision", {}))}
    return latest


def _option_decision_rows(decision_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for record in reversed(decision_records):
        if record.get("event") != "option_strategy_decision" or not isinstance(record.get("decision"), dict):
            continue
        decision = dict(record["decision"])
        ticker = str(decision.get("ticker", ""))
        if ticker and ticker not in latest:
            latest[ticker] = {"asof": record.get("ts"), **decision}
    return sorted(latest.values(), key=lambda item: item["ticker"])


def _safe_shadow_record(record: dict[str, Any]) -> dict[str, Any]:
    """Expose structured evidence, never raw provider reasoning_content."""
    decision = dict(record.get("decision") or {})
    challenge = dict(record.get("challenge") or {}) if isinstance(record.get("challenge"), dict) else None
    news = dict(record.get("news") or {}) if isinstance(record.get("news"), dict) else None
    return {
        "asof": record.get("ts"),
        "ticker": record.get("ticker"),
        "snapshot_id": record.get("snapshot_id"),
        "action": record.get("action"),
        "fail_closed": bool(record.get("fail_closed", False)),
        "risk_approved": bool(record.get("risk_approved", False)),
        "risk_reason": record.get("risk_reason"),
        "guardrail_actions": record.get("guardrail_actions", []),
        "model_calls": record.get("model_calls", 0),
        "decision": {
            key: decision.get(key)
            for key in ("thesis", "supporting_evidence", "contrary_evidence", "no_trade_reason", "confidence")
        },
        "challenge": None
        if challenge is None
        else {key: challenge.get(key) for key in ("recommendation", "veto_recommended", "objections", "contradictions", "missing_evidence", "stale_evidence")},
        "news": None
        if news is None
        else {key: news.get(key) for key in ("direction", "confidence", "source", "published_at", "events", "data_gaps")},
    }


def _safe_catalyst_record(record: dict[str, Any]) -> dict[str, Any]:
    decision = dict(record.get("decision") or {})
    challenge = dict(record.get("challenge") or {}) if isinstance(record.get("challenge"), dict) else {}
    bull = dict(record.get("bull_news") or {}) if isinstance(record.get("bull_news"), dict) else {}
    ranking = dict(record.get("ranking") or {}) if isinstance(record.get("ranking"), dict) else {}
    return {
        "asof": record.get("ts"),
        "ticker": record.get("ticker"),
        "final_action": record.get("final_action", record.get("action")),
        "instrument": record.get("instrument"),
        "risk_approved": bool(record.get("risk_approved", False)),
        "risk_reason": record.get("risk_reason"),
        "model_calls": record.get("model_calls", 0),
        "evidence_snapshot": record.get("evidence_snapshot"),
        "ranking": {key: ranking.get(key) for key in ("score", "direction", "rationale", "risk_flags")},
        "decision": {
            key: decision.get(key)
            for key in ("thesis", "supporting_evidence", "contrary_evidence", "confidence", "no_trade_reason")
        },
        "challenge": {
            key: challenge.get(key)
            for key in ("recommendation", "veto_recommended", "objections", "missing_evidence")
        },
        "bull_news": {
            key: bull.get(key)
            for key in ("catalyst_summary", "direction", "event_time", "source_urls", "data_gaps")
        },
    }


def _safe_ai_record(record: dict[str, Any]) -> dict[str, Any]:
    decision = dict(record.get("decision") or {})
    challenge = dict(record.get("challenge") or {}) if isinstance(record.get("challenge"), dict) else {}
    ranking = dict(record.get("ranking") or {}) if isinstance(record.get("ranking"), dict) else {}
    execution = dict(record.get("execution") or {})
    return {
        "asof": record.get("ts"),
        "ticker": record.get("ticker"),
        "ranking": {key: ranking.get(key) for key in ("score", "direction", "instrument_preference", "rationale")},
        "decision": {
            key: decision.get(key)
            for key in ("action", "instrument", "thesis", "confidence", "no_trade_reason")
        },
        "challenge": {
            key: challenge.get(key)
            for key in ("recommendation", "veto_recommended", "objections", "missing_evidence")
        },
        "execution": {
            key: execution.get(key)
            for key in ("status", "reason", "paper_sleeve", "live_order_tools_called")
        },
        "model_calls": record.get("model_calls", 0),
        "fail_closed": bool(record.get("fail_closed", False)),
    }


def _safe_allocator_decision(record: dict[str, Any]) -> dict[str, Any]:
    signal = dict(record.get("signal") or {})
    ranking = dict(record.get("ranking") or {})
    challenge = dict(record.get("challenge") or {})
    return {
        "asof": record.get("ts", record.get("decision_time")),
        "ticker": record.get("ticker"),
        "stage": record.get("stage"),
        "ranking": {
            key: ranking.get(key)
            for key in ("score", "rationale", "risk_flags")
        },
        "signal": {
            key: signal.get(key)
            for key in (
                "action",
                "horizon",
                "probability_status",
                "thesis",
                "entry_condition",
                "invalidation_condition",
                "max_holding_trading_days",
                "no_trade_reason",
                "watch_reason",
            )
        },
        "challenge": {
            key: challenge.get(key)
            for key in (
                "recommendation",
                "veto_recommended",
                "hard_veto",
                "concern_level",
                "hard_veto_reasons",
                "soft_concerns",
                "objections",
            )
        },
        "decision_outcome": record.get("decision_outcome"),
        "cooldown_transition": record.get("cooldown_transition"),
        "fail_closed": bool(record.get("fail_closed", False)),
    }


def _safe_allocator_allocation(record: dict[str, Any]) -> dict[str, Any]:
    return {
        key: record.get(key)
        for key in (
            "allocation_id",
            "plan_id",
            "decision_time",
            "data_cutoff_time",
            "status",
            "reason",
            "signal_summary",
            "considered",
            "selected_instrument",
            "counterfactual_2000",
            "short_equity_counterfactual",
            "probability_ev_available",
            "probability_ev_usd",
            "raw_probability_used_for_ev",
            "option_candidate_diagnostics",
        )
    }


def _record_date(record: dict[str, Any], *fields: str) -> str | None:
    for field in (*fields, "ts"):
        value = record.get(field)
        if isinstance(value, str) and len(value) >= 10:
            return value[:10]
    return None


def _session_records(
    records: list[dict[str, Any]],
    session_date: str,
    *fields: str,
) -> list[dict[str, Any]]:
    return [
        record
        for record in records
        if _record_date(record, *fields) == session_date
    ]


def _safe_trade_record(
    record: dict[str, Any],
    orders_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    result = {
        key: record.get(key)
        for key in (
            "symbol",
            "instrument",
            "entry_time",
            "exit_time",
            "entry_price",
            "exit_price",
            "quantity",
            "realized_pnl",
            "return_pct",
            "holding_minutes",
            "mae_pct",
            "mfe_pct",
            "outcome",
            "thesis",
        )
    }
    exit_order = orders_by_id.get(str(record.get("exit_order_id", "")), {})
    entry_order = orders_by_id.get(str(record.get("entry_order_id", "")), {})
    result["exit_reason"] = exit_order.get("thesis")
    result["binary_event_within_days"] = (
        entry_order.get("baseline_explanation") or {}
    ).get("binary_event_within_days")
    return result


def _build_beginner_summary(
    *,
    heartbeat: dict[str, Any],
    account: dict[str, Any],
    counters: dict[str, Any],
    metrics: dict[str, Any],
    positions: list[dict[str, Any]],
    option_positions: list[dict[str, Any]],
    orders: list[dict[str, Any]],
    audit_records: list[dict[str, Any]],
    runtime_job_records: list[dict[str, Any]],
    decision_records: list[dict[str, Any]],
    trade_records: list[dict[str, Any]],
    option_diagnostics: list[dict[str, Any]],
    ai_cycle_records: list[dict[str, Any]],
    llm_usage_records: list[dict[str, Any]],
) -> dict[str, Any]:
    heartbeat_payload = _as_dict(heartbeat.get("payload"))
    latest_jobs = _as_dict(heartbeat_payload.get("latest_jobs"))
    forward_job = _as_dict(latest_jobs.get("forward"))
    forward_output = _as_dict(forward_job.get("output"))
    forward_clock = _as_dict(forward_output.get("clock"))
    session_date = str(
        forward_clock.get("session")
        or counters.get("date")
        or ""
    )
    if not session_date:
        dated = [
            value
            for record in trade_records
            for value in (
                _record_date(record, "exit_time", "entry_time"),
            )
            if value
        ]
        session_date = max(dated, default=utc_now()[:10])

    session_trades = _session_records(
        trade_records,
        session_date,
        "exit_time",
        "entry_time",
    )
    opened = [
        record
        for record in session_trades
        if record.get("event") == "trade_opened"
        and _record_date(record, "entry_time") == session_date
    ]
    closed = [
        record
        for record in session_trades
        if record.get("event") == "trade_closed"
        and _record_date(record, "exit_time") == session_date
    ]
    daily_pnl = (
        float(counters.get("daily_realized_pnl", 0))
        if str(counters.get("date")) == session_date
        else sum(float(record.get("realized_pnl") or 0) for record in closed)
    )
    wins = sum(float(record.get("realized_pnl") or 0) > 0 for record in closed)
    losses = sum(float(record.get("realized_pnl") or 0) < 0 for record in closed)
    orders_by_id = {
        str(order.get("order_id")): order
        for order in orders
        if order.get("order_id")
    }

    session_audit = _session_records(audit_records, session_date)
    regular_cycles = [
        record
        for record in session_audit
        if record.get("event") in {"forward_cycle_complete", "forward_cycle_exit_only"}
    ]
    latest_regular_cycle = regular_cycles[-1] if regular_cycles else None
    session_ai_cycles = _session_records(ai_cycle_records, session_date)
    ai_failures = [
        record
        for record in session_ai_cycles
        if record.get("event") == "ai_gated_cycle_failed_closed"
    ]
    ai_completed = [
        record
        for record in session_ai_cycles
        if record.get("event") == "ai_gated_cycle_complete"
    ]
    session_option_diagnostics = _session_records(option_diagnostics, session_date)
    session_option_direction_decisions = [
        record
        for record in _session_records(decision_records, session_date)
        if record.get("event") == "option_strategy_decision"
    ]
    option_rejections: Counter[str] = Counter()
    for record in session_option_diagnostics:
        diagnostics = record.get("diagnostics", {})
        if isinstance(diagnostics, dict):
            option_rejections.update(
                {
                    str(reason): int(count)
                    for reason, count in dict(
                        diagnostics.get("rejections", {})
                    ).items()
                }
            )

    session_jobs = [
        record
        for record in _session_records(runtime_job_records, session_date, "finished_at")
        if record.get("event") == "runtime_job_finished"
    ]
    failed_jobs = [
        record
        for record in session_jobs
        if record.get("status") != "completed"
        or record.get("returncode") not in (0, None)
    ]
    data_warnings = [
        record
        for record in session_audit
        if record.get("event")
        in {
            "intraday_volume_failed_closed",
            "earnings_calendar_unavailable",
            "entry_quote_refresh_failed_closed",
            "option_entry_failed_closed",
        }
    ]

    issues: list[dict[str, Any]] = []
    if ai_failures:
        issues.append(
            {
                "code": "ai_structured_output_failed",
                "severity": "error",
                "count": len(ai_failures),
                "title": "AI 独立策略没有完成决策",
                "impact": (
                    f"{len(ai_failures)} 次循环停在模型排名阶段，"
                    "因此没有进入研究或模拟下单。"
                ),
                "resolution": "已扩大输出预算，并为截断 JSON 增加自适应重试。",
            }
        )
    future_quote_rejections = int(
        option_rejections.get("future option quote would create lookahead", 0)
    )
    if future_quote_rejections:
        issues.append(
            {
                "code": "option_quote_observation_time",
                "severity": "error",
                "count": future_quote_rejections,
                "title": "期权报价被时间校验误拒绝",
                "impact": (
                    f"{future_quote_rejections} 份新报价被当成未来数据，"
                    "所以期权线没有可选合约。"
                ),
                "resolution": "已改用网络响应后的本地观察时间，真正的未来报价仍会被拒绝。",
            }
        )
    if failed_jobs:
        issues.append(
            {
                "code": "runtime_job_failures",
                "severity": "error",
                "count": len(failed_jobs),
                "title": "调度作业失败",
                "impact": "至少一个受监督作业未正常完成。",
                "resolution": "查看高级运行详情中的作业名称和安全错误摘要。",
            }
        )
    if data_warnings:
        issues.append(
            {
                "code": "market_data_warning",
                "severity": "warning",
                "count": len(data_warnings),
                "title": "行情数据曾短暂不可用",
                "impact": (
                    f"{len(data_warnings)} 个周期按安全规则跳过，"
                    "没有使用缺失数据做决定。"
                ),
                "resolution": "后续周期已恢复；该行为属于预期的 fail-closed。",
            }
        )

    session_usage = _session_records(llm_usage_records, session_date)
    regular_clock = _as_dict(
        latest_regular_cycle.get("clock", {})
        if isinstance(latest_regular_cycle, dict)
        else {}
    )
    if regular_clock.get("open_time") and regular_clock.get("close_time"):
        open_time = parse_ts(str(regular_clock["open_time"]))
        close_time = parse_ts(str(regular_clock["close_time"]))
        session_usage = [
            record
            for record in session_usage
            if record.get("ts")
            and open_time <= parse_ts(str(record["ts"])) <= close_time
        ]
    usage_errors = [
        record for record in session_usage if record.get("error")
    ]
    if usage_errors:
        issues.append(
            {
                "code": "llm_stage_errors",
                "severity": "warning",
                "count": len(usage_errors),
                "title": "部分 AI 阶段超时或输出无效",
                "impact": (
                    f"{len(usage_errors)} 次模型调用安全失败；"
                    "对应候选没有进入模拟下单。"
                ),
                "resolution": (
                    "已按 Agent 配置更长超时；失败仍会保持 no-trade。"
                ),
            }
        )
    usage_cost = sum(
        float(record.get("estimated_cost_usd") or 0)
        for record in session_usage
    )
    evidence_thresholds = metrics.get("evaluation_thresholds", {})
    minimum_sessions = int(
        evidence_thresholds.get("minimum_forward_sessions", 20)
    )
    minimum_trades = int(
        evidence_thresholds.get("minimum_closed_trades", 30)
    )
    current_session = forward_clock.get("market_session")
    if current_session is None:
        latest_clock_record = _last(
            session_audit,
            lambda record: isinstance(record.get("clock"), dict),
        )
        current_session = _as_dict(
            (latest_clock_record or {}).get("clock")
        ).get("market_session")

    return {
        "session_date": session_date,
        "service": {
            "status": heartbeat.get("effective_status", "unknown"),
            "heartbeat_age_seconds": heartbeat.get("age_seconds"),
            "market_session": current_session,
        },
        "day": {
            "realized_pnl": round(daily_pnl, 4),
            "result": (
                "profit"
                if daily_pnl > 0
                else "loss"
                if daily_pnl < 0
                else "flat"
            ),
            "entries": len(opened),
            "closed_trades": len(closed),
            "wins": wins,
            "losses": losses,
            "trades": [
                _safe_trade_record(record, orders_by_id)
                for record in closed
            ],
            "regular_cycles": len(regular_cycles),
        },
        "account": {
            "initial_cash": account.get("initial_cash"),
            "ending_equity": metrics.get("ending_equity"),
            "cash": account.get("cash"),
            "cumulative_pnl": account.get("realized_pnl"),
            "cumulative_return_pct": metrics.get("net_return_pct"),
            "open_equity_positions": len(positions),
            "open_option_positions": len(option_positions),
        },
        "evidence": {
            "status": metrics.get("profitability"),
            "sufficient": bool(metrics.get("evidence_sufficient", False)),
            "promotion_eligible": bool(
                metrics.get("promotion_eligible", False)
            ),
            "forward_sessions": int(metrics.get("forward_session_count", 0)),
            "minimum_forward_sessions": minimum_sessions,
            "closed_trades": int(metrics.get("closed_trade_count", 0)),
            "minimum_closed_trades": minimum_trades,
            "win_rate": metrics.get("win_rate"),
            "profit_factor": metrics.get("profit_factor"),
            "max_drawdown_pct": metrics.get("max_drawdown_pct"),
        },
        "strategy_lines": {
            "equity": {
                "watchlist_count": len(
                    {
                        str(record.get("active_strategy", {}).get("ticker", ""))
                        for record in _session_records(
                            decision_records,
                            session_date,
                        )
                        if record.get("event") == "strategy_comparison"
                        and record.get("active_strategy", {}).get("ticker")
                    }
                ),
                "entries": len(opened),
                "closed_trades": len(closed),
                "earnings_risk_entries": sum(
                    record.get("binary_event_within_days") is not None
                    and int(record["binary_event_within_days"]) <= 1
                    for record in (
                        _safe_trade_record(item, orders_by_id)
                        for item in closed
                    )
                ),
                "daily_pnl": round(
                    sum(
                        float(record.get("realized_pnl") or 0)
                        for record in closed
                        if record.get("instrument") == "equity"
                    ),
                    4,
                ),
                "status": "traded" if opened else "observed",
            },
            "options": {
                "direction_evaluations": len(
                    session_option_direction_decisions
                ),
                "selection_attempts": len(session_option_diagnostics),
                "orders": int(counters.get("option_trades", 0)),
                "status": (
                    "validation_error"
                    if future_quote_rejections
                    else "observed"
                ),
                "top_rejections": dict(option_rejections.most_common(5)),
            },
            "ai": {
                "cycles": len(session_ai_cycles),
                "completed": len(ai_completed),
                "failed": len(ai_failures),
                "latest_candidate_count": (
                    int(ai_completed[-1].get("technical_candidate_count", 0))
                    if ai_completed
                    else 0
                ),
                "latest_top_set_count": (
                    len(ai_completed[-1].get("technical_top_set", []))
                    if ai_completed
                    else 0
                ),
                "status": (
                    "failed_closed"
                    if ai_failures and not ai_completed
                    else "completed"
                    if ai_completed
                    else "observed"
                ),
            },
        },
        "issues": issues,
        "operations": {
            "runtime_jobs": len(session_jobs),
            "failed_jobs": len(failed_jobs),
            "llm_calls": len(session_usage),
            "llm_errors": len(usage_errors),
            "estimated_api_cost_usd": round(usage_cost, 6),
            "historical_cost_incomplete": any(
                record.get("error")
                and not record.get("input_tokens")
                and not record.get("output_tokens")
                for record in session_usage
            ),
            "latest_regular_cycle": (
                None
                if latest_regular_cycle is None
                else {
                    key: latest_regular_cycle.get(key)
                    for key in (
                        "ts",
                        "event",
                        "quotes",
                        "snapshots",
                        "active_candidates",
                        "selected_candidates",
                    )
                }
            ),
        },
    }


def build_dashboard_state(root: str | Path) -> dict[str, Any]:
    """Build a bounded, JSON-safe view of existing local state and logs."""
    root_path = Path(root).resolve()
    state_dir = root_path / "state"
    logs_dir = root_path / "logs"
    runtime = load_runtime_config(root_path)
    audit_records = _read_jsonl(logs_dir / "audit.jsonl", limit=5000)
    decision_records = _read_jsonl(logs_dir / "decisions.jsonl", limit=2500)
    shadow_records = _read_jsonl(logs_dir / "shadow_decisions.jsonl", limit=100)
    catalyst_discovery_records = _read_jsonl(logs_dir / "catalyst_discovery.jsonl", limit=50)
    catalyst_decision_records = _read_jsonl(logs_dir / "catalyst_decisions.jsonl", limit=100)
    ai_cycle_records = _read_jsonl(logs_dir / "ai_gated_cycles.jsonl", limit=50)
    ai_decision_records = _read_jsonl(logs_dir / "ai_gated_decisions.jsonl", limit=100)
    news_drift_cycles = _read_jsonl(logs_dir / "news_drift_cycles.jsonl", limit=100)
    outcome_records = _read_jsonl(logs_dir / "candidate_outcomes.jsonl", limit=500)
    option_diagnostics = _read_jsonl(logs_dir / "option_selection_diagnostics.jsonl", limit=100)
    runtime_job_records = _read_jsonl(logs_dir / "runtime_jobs.jsonl", limit=3000)
    trade_records = _read_jsonl(logs_dir / "trade_journal.jsonl", limit=1000)
    llm_usage_records = _read_jsonl(logs_dir / "llm_usage.jsonl", limit=1000)
    candidates, decisions_by_snapshot = _candidate_rows(decision_records)
    exit_by_symbol = _exit_rows(decision_records)
    option_decisions = _option_decision_rows(decision_records)
    shadow_by_snapshot: dict[str, dict[str, Any]] = {}
    for record in reversed(shadow_records):
        snapshot_id = str(record.get("snapshot_id", ""))
        if snapshot_id and snapshot_id not in shadow_by_snapshot:
            shadow_by_snapshot[snapshot_id] = _safe_shadow_record(record)

    orders_raw = _read_json(state_dir / "paper_orders.json", {})
    orders = _dict_values(orders_raw)
    orders.sort(key=lambda item: str(item.get("submitted_at") or item.get("created_at") or ""), reverse=True)
    for order in orders:
        snapshot_id = str(order.get("decision_id", ""))
        order["baseline_explanation"] = decisions_by_snapshot.get(snapshot_id)
        order["shadow_explanation"] = shadow_by_snapshot.get(snapshot_id)

    positions_raw = _read_json(state_dir / "paper_positions.json", {})
    positions = _dict_values(positions_raw)
    for position in positions:
        position["exit_evaluation"] = exit_by_symbol.get(str(position.get("symbol", "")))

    option_orders_raw = _read_json(state_dir / "paper_option_orders.json", {})
    option_orders = _dict_values(option_orders_raw)
    option_orders.sort(key=lambda item: str(item.get("submitted_at") or item.get("created_at") or ""), reverse=True)
    option_positions_raw = _read_json(state_dir / "paper_option_positions.json", {})
    option_positions = _dict_values(option_positions_raw)

    latest_cycle = _last(audit_records, lambda item: item.get("event") in {"forward_cycle_complete", "forward_cycle_exit_only", "forward_cycle_skipped", "forward_cycle_failed_closed"})
    last_shadow = _safe_shadow_record(shadow_records[-1]) if shadow_records else None
    paper = runtime["paper"]
    risk = runtime["risk"]
    thinking = runtime["llm"].get("api", {}).get("thinking")
    now = utc_now()
    heartbeat = _as_dict(_read_json(state_dir / "runtime_heartbeat.json", {}))
    heartbeat_age = None
    heartbeat_time_valid = False
    if heartbeat.get("last_heartbeat_at"):
        try:
            raw_heartbeat_age = (
                parse_ts(now) - parse_ts(str(heartbeat["last_heartbeat_at"]))
            ).total_seconds()
        except (TypeError, ValueError):
            pass
        else:
            heartbeat_age = max(0.0, raw_heartbeat_age)
            heartbeat_time_valid = raw_heartbeat_age >= 0
    stale_after = int(runtime.get("integrations", {}).get("runtime", {}).get("watchdog_max_age_seconds", 900))
    heartbeat["age_seconds"] = round(heartbeat_age, 1) if heartbeat_age is not None else None
    heartbeat["stale"] = (
        not heartbeat_time_valid
        or heartbeat_age is None
        or heartbeat_age > stale_after
    )
    heartbeat["service_lock"] = ProcessLock.inspect(state_dir / "forward_service.lock")
    if heartbeat["stale"]:
        heartbeat["effective_status"] = "stale"
    elif heartbeat["service_lock"]["alive"] is not True:
        heartbeat["effective_status"] = "stopped"
    else:
        heartbeat["effective_status"] = heartbeat.get("status", "unknown")
    ai_namespace = str(runtime.get("strategies", {}).get("ai_gated_technical_v1", {}).get("state_namespace", "ai_gated_technical_v1"))
    ai_state_dir = state_dir / "strategy_sleeves" / ai_namespace
    ai_account_exists = (ai_state_dir / "paper_account.json").exists()
    ai_orders_raw = _read_json(ai_state_dir / "paper_orders.json", {}) if ai_account_exists else {}
    ai_option_orders_raw = _read_json(ai_state_dir / "paper_option_orders.json", {}) if ai_account_exists else {}
    allocator_namespace = str(
        runtime.get("strategies", {})
        .get("ai_instrument_allocator_v1", {})
        .get("state_namespace", "ai_instrument_allocator_v1")
    )
    allocator_state_dir = state_dir / "strategy_sleeves" / allocator_namespace
    allocator_log_dir = logs_dir / "strategy_sleeves" / allocator_namespace
    allocator_state = AllocatorStateStore(
        root_path,
        namespace=allocator_namespace,
    )
    allocator_account_exists = (allocator_state_dir / "paper_account.json").exists()
    allocator_orders_raw = (
        _read_json(allocator_state_dir / "paper_orders.json", {})
        if allocator_account_exists
        else {}
    )
    allocator_option_orders_raw = (
        _read_json(allocator_state_dir / "paper_option_orders.json", {})
        if allocator_account_exists
        else {}
    )
    allocator_allocations = _read_jsonl(
        allocator_log_dir / "allocations.jsonl",
        limit=100,
    )
    allocator_decisions = _read_jsonl(
        allocator_log_dir / "decisions.jsonl",
        limit=100,
    )
    allocator_cycles = _read_jsonl(
        allocator_log_dir / "cycles.jsonl",
        limit=100,
    )
    allocator_fill_records = [
        *_read_jsonl(allocator_log_dir / "paper_fills.jsonl", limit=1000),
        *_read_jsonl(
            allocator_log_dir / "paper_option_fills.jsonl",
            limit=1000,
        ),
    ]
    allocator_policy_replay_artifact = _as_dict(
        _read_json(
            allocator_state_dir / "allocator_policy_replay_latest.json",
            {},
        )
    )
    allocator_policy_replay = _safe_allocator_policy_replay(
        root_path,
        allocator_namespace,
        allocator_policy_replay_artifact,
    )
    allocator_validation_report = _as_dict(
        _read_json(
            root_path / "reports" / "allocator_validation_latest.json",
            {},
        )
    )
    short_counterfactuals = _read_jsonl(
        allocator_log_dir / "short_equity_counterfactual.jsonl",
        limit=100,
    )
    account = _as_dict(_read_json(state_dir / "paper_account.json", {}))
    counters = _as_dict(_read_json(state_dir / "daily_counters.json", {}))
    metrics = _safe_metrics(root_path)
    allocator_metrics = (
        _safe_metrics(root_path, namespace=allocator_namespace)
        if allocator_account_exists
        else None
    )
    allocator_validation = _allocator_validation_view(
        allocator_validation_report,
        allocator_metrics,
    )
    beginner_summary = _build_beginner_summary(
        heartbeat=heartbeat,
        account=account,
        counters=counters,
        metrics=metrics,
        positions=positions,
        option_positions=option_positions,
        orders=orders,
        audit_records=audit_records,
        runtime_job_records=runtime_job_records,
        decision_records=decision_records,
        trade_records=trade_records,
        option_diagnostics=option_diagnostics,
        ai_cycle_records=ai_cycle_records,
        llm_usage_records=llm_usage_records,
    )
    trade_funnel = _build_trade_funnel(
        now=now,
        allocator_profile=_as_dict(
            runtime.get("strategies", {}).get("ai_instrument_allocator_v1")
        ),
        audit_records=audit_records,
        allocator_cycles=allocator_cycles,
        allocator_decisions=allocator_decisions,
        allocator_fill_records=allocator_fill_records,
        allocator_policy_replay=allocator_policy_replay,
    )
    market_data = _market_data_observations(
        decision_records,
        option_diagnostics,
        now=now,
        equity_stale_after_seconds=int(paper.get("quote_stale_after_seconds", 60)),
        option_stale_after_seconds=int(
            runtime.get("options_costs", {}).get("quote_stale_after_seconds", 30)
        ),
    )
    display_orders, main_order_counts = _bounded_orders(orders)
    display_option_orders, main_option_order_counts = _bounded_orders(option_orders)
    ai_orders = _dict_values(ai_orders_raw)
    ai_option_orders = _dict_values(ai_option_orders_raw)
    display_ai_orders, ai_order_counts = _bounded_orders(ai_orders)
    display_ai_option_orders, ai_option_order_counts = _bounded_orders(
        ai_option_orders
    )
    allocator_orders = _dict_values(allocator_orders_raw)
    allocator_option_orders = _dict_values(allocator_option_orders_raw)
    display_allocator_orders, allocator_order_counts = _bounded_orders(
        allocator_orders
    )
    display_allocator_option_orders, allocator_option_order_counts = _bounded_orders(
        allocator_option_orders
    )
    order_count_groups = (
        main_order_counts,
        main_option_order_counts,
        ai_order_counts,
        ai_option_order_counts,
        allocator_order_counts,
        allocator_option_order_counts,
    )
    order_history_summary = {
        key: sum(group[key] for group in order_count_groups)
        for key in ("open_total", "completed_total", "completed_included")
    }
    return {
        "mode": {
            "paper": bool(paper.get("mode", {}).get("paper", False)),
            "live_readonly": bool(
                paper.get("mode", {}).get("live_readonly", False)
            ),
            "live_trading": bool(
                paper.get("mode", {}).get("live_trading", False)
            ),
        },
        "heartbeat": heartbeat,
        "market_data": market_data,
        "account": account,
        "daily_counters": counters,
        "positions": positions,
        "option_positions": option_positions,
        "orders": display_orders,
        "option_orders": display_option_orders,
        "order_history_summary": order_history_summary,
        "latest_cycle": latest_cycle,
        "strategy_modes": {
            "weighted_relative_strength_v2": str(
                runtime.get("strategies", {})
                .get("weighted_relative_strength_v2", {})
                .get("execution", "shadow_only")
            ),
            "long_directional_options_v2_weighted": str(
                runtime.get("strategies", {})
                .get("long_directional_options_v2_weighted", {})
                .get("execution", "shadow_only")
            ),
            "ai_gated_technical_v1_new_entries": bool(
                runtime.get("strategies", {})
                .get("ai_gated_technical_v1", {})
                .get("new_entries_enabled", False)
            ),
            "long_directional_options_v2_weighted_new_entries": bool(
                runtime.get("strategies", {})
                .get("long_directional_options_v2_weighted", {})
                .get("new_entries_enabled", False)
            ),
            "ai_instrument_allocator_v1": str(
                runtime.get("strategies", {})
                .get("ai_instrument_allocator_v1", {})
                .get("execution", "disabled")
            ),
        },
        "candidates": candidates,
        "option_decisions": option_decisions,
        "metrics": metrics,
        "beginner_summary": beginner_summary,
        "trade_funnel": trade_funnel,
        "allocator_validation": allocator_validation,
        "last_shadow_decision": last_shadow,
        "latest_catalyst_discovery": catalyst_discovery_records[-1] if catalyst_discovery_records else None,
        "catalyst_decisions": [_safe_catalyst_record(record) for record in catalyst_decision_records[-20:]],
        "adaptive_weights": _read_json(
            state_dir
            / str(
                runtime.get("strategies", {})
                .get("weighted_relative_strength_v2", {})
                .get("weight_state_file", "strategy_weights.json")
            ),
            {},
        ),
        "outcomes": {
            "resolved_count": len(outcome_records),
            "profitable_count": sum(bool(item.get("profitable_after_spread")) for item in outcome_records),
            "latest": outcome_records[-20:],
        },
        "option_selection_diagnostics": option_diagnostics[-20:],
        "ai_gated": {
            "namespace": ai_namespace,
            "account": _read_json(ai_state_dir / "paper_account.json", {}),
            "positions": _dict_values(_read_json(ai_state_dir / "paper_positions.json", {})) if ai_account_exists else [],
            "option_positions": _dict_values(_read_json(ai_state_dir / "paper_option_positions.json", {})) if ai_account_exists else [],
            "orders": display_ai_orders,
            "option_orders": display_ai_option_orders,
            "metrics": _safe_metrics(root_path, namespace=ai_namespace)
            if ai_account_exists
            else None,
            "latest_cycle": ai_cycle_records[-1] if ai_cycle_records else None,
            "decisions": [_safe_ai_record(record) for record in ai_decision_records[-20:]],
        },
        "ai_instrument_allocator": {
            "namespace": allocator_namespace,
            "account": _read_json(
                allocator_state_dir / "paper_account.json",
                {},
            ),
            "positions": _dict_values(
                _read_json(allocator_state_dir / "paper_positions.json", {})
            )
            if allocator_account_exists
            else [],
            "option_positions": _dict_values(
                _read_json(allocator_state_dir / "paper_option_positions.json", {})
            )
            if allocator_account_exists
            else [],
            "orders": display_allocator_orders,
            "option_orders": display_allocator_option_orders,
            "metrics": allocator_metrics,
            "latest_cycle": allocator_cycles[-1] if allocator_cycles else None,
            "latest_allocation": _safe_allocator_allocation(
                allocator_allocations[-1]
            )
            if allocator_allocations
            else None,
            "decisions": [
                _safe_allocator_decision(record)
                for record in allocator_decisions[-20:]
            ],
            "plans": _dict_values(
                _read_json(allocator_state_dir / "allocator_plans.json", {})
            ),
            "watches": allocator_state.active_watches(now),
            "mandates": _dict_values(
                _read_json(allocator_state_dir / "position_mandates.json", {})
            ),
            "short_equity_counterfactual": short_counterfactuals[-1]
            if short_counterfactuals
            else None,
        },
        "news_drift": {
            "metrics": _safe_news_drift_metrics(root_path),
            "latest_cycle": news_drift_cycles[-1] if news_drift_cycles else None,
        },
        "safety": {
            "allow_options": bool(risk.get("allow_options", False)),
            "allow_fractional_shares": bool(risk.get("allow_fractional_shares", False)),
            "fractional_share_increment": risk.get("fractional_share_increment"),
            "max_order_pct_of_equity": risk.get("max_order_pct_of_equity"),
            "max_open_positions": risk.get("max_open_positions"),
            "max_daily_trades": risk.get("max_daily_trades"),
            "options_risk": runtime.get("options_risk", {}),
            "shared_risk": runtime.get("shared_risk", {}),
            "thinking": thinking,
        },
    }


_PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Paper Trading Control Room</title>
<style>
:root{color-scheme:dark;--bg:#0b1020;--panel:#151d35;--line:#293550;--text:#e9eefb;--muted:#a5b1cc;--ok:#55dba6;--warn:#ffcb6b;--bad:#ff7272;--blue:#82aaff}*{box-sizing:border-box}body{font:14px/1.45 Inter,Segoe UI,Arial,sans-serif;background:var(--bg);color:var(--text);margin:0;padding:22px}h1{font-size:24px;margin:0}h2{font-size:16px;margin:0 0 10px}p{margin:5px 0;color:var(--muted)}.grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));margin:14px 0}.panel{border:1px solid var(--line);background:var(--panel);border-radius:8px;padding:15px;overflow:auto}.wide{grid-column:1/-1}.metric{font-size:22px;font-weight:650}.tag{display:inline-block;border-radius:999px;padding:2px 8px;background:#263352;color:var(--text);margin:2px}.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}table{border-collapse:collapse;width:100%;min-width:740px}th,td{text-align:left;vertical-align:top;border-bottom:1px solid var(--line);padding:8px}th{color:var(--muted);font-weight:600}ul{margin:6px 0;padding-left:18px}.mono{font-family:Consolas,monospace;font-size:12px}.small{font-size:12px;color:var(--muted)}button{background:#415a9d;color:white;border:0;border-radius:6px;padding:8px 12px;cursor:pointer}details{margin:6px 0}summary{cursor:pointer;color:var(--blue)}#updated{color:var(--muted);font-size:12px;margin-left:10px}@media(max-width:640px){body{padding:12px}.wide{grid-column:auto}}</style></head>
<body><header><h1>Paper Trading Control Room <span id="updated"></span></h1><p>只读本地审计界面；没有下单按钮，也不会访问 Robinhood 交易工具。</p></header><main id="app">加载中…</main>
<script>
const esc=v=>String(v??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
const pct=v=>v===undefined||v===null?'—':Number(v).toFixed(4)+'%'; const money=v=>v===undefined||v===null?'—':'$'+Number(v).toFixed(2);
const list=v=>Array.isArray(v)&&v.length?'<ul>'+v.map(x=>'<li>'+esc(x)+'</li>').join('')+'</ul>':'—';
const lineStatus=l=>`<span class="tag ${l.promotion_eligible?'ok':'warn'}">${esc(l.profitability||'insufficient_forward_evidence')}</span>`;
function cycle(d){let c=d.latest_cycle||{},clock=c.clock||{};return `<div class="panel"><h2>最近周期</h2><p><b>${esc(c.event||'尚无周期')}</b> · ${esc(clock.asof||'—')}</p><p>行情 ${esc(c.quotes??'—')} · 快照 ${esc(c.snapshots??'—')} · 通过筛选 ${esc(c.baseline_candidates??'—')}</p><p>入选：${list(c.selected_candidates||[])}</p><p class="small">订单 ${esc((c.orders||[]).length)} · 退出 ${esc((c.exits||[]).length)}</p></div>`}
function portfolio(d){let a=d.account||{},pos=d.positions||[],c=d.daily_counters||{},m=d.metrics||{},lines=m.lines||{};return `<div class="panel"><h2>共享纸面账户</h2><div class="metric">${money(a.cash)} 现金</div><p>净值 ${money(m.ending_equity)} · 已实现 ${money(a.realized_pnl)}</p><p>今日入场 ${esc(c.trades??0)} 笔；股票 ${esc(c.equity_trades??0)} / 期权 ${esc(c.option_trades??0)}</p><p>股票 PnL ${money((lines.equity||{}).net_pnl)} · 期权 PnL ${money((lines.options||{}).net_pnl)}</p><p>股票证据：${lineStatus(lines.equity||{})}</p><h3>股票持仓</h3>${pos.length?pos.map(p=>`<details open><summary>${esc(p.symbol)} · ${esc(p.quantity)} 股 @ ${money(p.average_price)}</summary><p>开仓：${esc(p.opened_at)}</p><p>退出判断：${esc((p.exit_evaluation||{}).reason||'尚无记录')}</p></details>`).join(''):'<p>无股票持仓</p>'}</div>`}
function safety(d){let s=d.safety||{},t=s.thinking||{},o=s.options_risk||{},r=s.shared_risk||{};return `<div class="panel"><h2>边界与模型</h2><p><span class="tag ok">纸面模式</span> <span class="tag ${s.allow_options?'ok':'bad'}">期权纸面：${s.allow_options?'启用':'禁止'}</span></p><p>共享总占用 ≤ ${Number(r.max_total_deployed_pct_of_equity||0)*100}%；股票 ≤ ${Number(r.max_equity_deployed_pct_of_equity||0)*100}%；期权 ≤ ${Number(r.max_options_deployed_pct_of_equity||0)*100}%</p><p>期权仅长 call/put；单笔权利金风险 ≤ ${Number(o.max_order_risk_pct_of_equity||0)*100}%；禁止卖方、保证金、行权</p><p>分数股：${s.allow_fractional_shares?'允许，每 '+esc(s.fractional_share_increment)+' 股递增':'禁止'}</p><p>Thinking：新闻 ${esc((((t.agents||{}).news_agent||t.default||{}).type||'默认'))}；质询 ${esc((((t.agents||{}).challenge_agent||t.default||{}).type||'默认'))}；决策 ${esc((((t.agents||{}).decision_manager||t.default||{}).type||'默认'))}</p></div>`}
function optionLine(d){let pos=d.option_positions||[],orders=d.option_orders||[],dec=d.option_decisions||[],line=((d.metrics||{}).lines||{}).options||{},g=(((d.latest_cycle||{}).portfolio||{}).option_greeks||{});let ds=dec.map(x=>`<tr><td>${esc(x.ticker)}</td><td>${esc(x.option_type||'—')}</td><td class="${x.action==='buy_to_open'?'ok':'warn'}">${esc(x.action)}</td><td>${list(x.reasons)}</td></tr>`).join('');return `<section class="panel wide"><h2>期权策略线 · ${esc(line.closed_trade_count||0)} 笔平仓 · 胜率 ${pct(Number(line.win_rate||0)*100)} · PnL ${money(line.net_pnl)}</h2><p>独立证据：${lineStatus(line)} · 不借用股票交易样本</p><p>组合 Greeks：Delta ${esc(g.delta??'—')} · Gamma ${esc(g.gamma??'—')} · Theta/日 ${esc(g.theta_per_day??'—')} · Vega/1% ${esc(g.vega_per_vol_point??'—')}</p><p>${pos.length?pos.map(p=>{let c=p.contract||{};return `${esc(c.underlying)} ${esc(c.expiration_date)} ${esc(c.strike_price)}${esc((c.option_type||'')[0]||'')} · ${esc(p.quantity)} 张 @ ${money(p.average_price)}`}).join('<br>'):'无期权持仓'}</p><table><thead><tr><th>标的</th><th>方向</th><th>决策</th><th>原因</th></tr></thead><tbody>${ds||'<tr><td colspan="4">尚无期权筛选记录</td></tr>'}</tbody></table><h3>期权订单</h3>${orders.length?orders.map(o=>{let c=o.contract||{};return `<details><summary>${esc(c.underlying)} ${esc(c.expiration_date)} ${esc(c.strike_price)}${esc((c.option_type||'')[0]||'')} · ${esc(o.intent)} · ${esc(o.status)}</summary><p>${esc(o.quantity)} 张 @ ${money(o.average_fill_price||o.limit_price)}；${esc(o.reject_reason||o.thesis||'')}</p></details>`}).join(''):'<p>尚无期权订单</p>'}</section>`}
function candidates(d){let rows=(d.candidates||[]).map(x=>{let t=x.technical||{},r=x.regime||{};return `<tr><td>${esc(x.ticker)}</td><td class="${x.action==='buy'?'ok':'warn'}">${esc(x.action)}</td><td>${esc(r.status)} / ${r.eligible?'可':'否'}</td><td>${pct(t.relative_strength_20d)}</td><td>${pct(t.price_change_5d_pct)}</td><td>${Number(t.volume_ratio??0).toFixed(2)}</td><td>${list(x.reasons)}</td></tr>`}).join('');return `<section class="panel wide"><h2>十标的硬筛选：为什么买 / 为什么不买</h2><table><thead><tr><th>标的</th><th>结果</th><th>市场状态</th><th>20日相对强度</th><th>5日变化</th><th>量比</th><th>可审计原因</th></tr></thead><tbody>${rows||'<tr><td colspan="7">尚无基线决策记录</td></tr>'}</tbody></table></section>`}
function orders(d){let rows=(d.orders||[]).map(o=>{let b=o.baseline_explanation||{},sh=o.shadow_explanation||{},t=(b.technical||{});let shadow=sh.decision||{};return `<details><summary>${esc(o.symbol)} ${esc(o.side)} ${esc(o.filled_quantity||o.quantity)} 股 · ${esc(o.status)} · ${money(o.average_fill_price||o.limit_price)}</summary><p>订单 ID：<span class="mono">${esc(o.order_id)}</span></p><p>基线策略：${esc(o.thesis)}。当时 RS20 ${pct(t.relative_strength_20d)}，5日 ${pct(t.price_change_5d_pct)}，量比 ${esc(t.volume_ratio??'—')}。</p><p>影子判断：${esc(sh.action||'未运行')}；风控：${esc(sh.risk_reason||'—')}；模型调用 ${esc(sh.model_calls??'—')}。</p><p>结构化投资论点：${esc(shadow.thesis||'—')}</p><p>支持证据：${list(shadow.supporting_evidence)} 反证：${list(shadow.contrary_evidence)}</p><p>质询：${list((sh.challenge||{}).objections)}；保护动作：${list(sh.guardrail_actions)}</p></details>`}).join('');return `<section class="panel wide"><h2>订单与可解释链路</h2>${rows||'<p>尚无纸面订单。</p>'}</section>`}
function shadow(d){let x=d.last_shadow_decision;if(!x)return '<section class="panel wide"><h2>最近影子研究</h2><p>尚无影子决策。</p></section>';let z=x.decision||{};return `<section class="panel wide"><h2>最近影子研究（结构化理由，不显示原始私有 CoT）</h2><p>${esc(x.ticker)}：<b>${esc(x.action)}</b> · 风控 ${esc(x.risk_reason)} · ${x.fail_closed?'失败关闭':'正常完成'}</p><p>论点：${esc(z.thesis)}；置信度：${esc(z.confidence)}</p><p>支持：${list(z.supporting_evidence)} 反证：${list(z.contrary_evidence)}</p><p>质询：${list((x.challenge||{}).objections)}；缺失证据：${list((x.challenge||{}).missing_evidence)}</p></section>`}
function catalyst(d){let rows=(d.catalyst_decisions||[]).slice().reverse().map(x=>{let z=x.decision||{},b=x.bull_news||{},r=x.ranking||{};return `<tr><td>${esc(x.asof||'—')}</td><td>${esc(x.ticker)}</td><td>${esc(x.instrument)}</td><td class="${x.risk_approved?'ok':'warn'}">${esc(x.final_action)}</td><td>${Number(r.score??0).toFixed(3)}</td><td>${esc(z.thesis||b.catalyst_summary||'—')}</td><td>${esc(x.risk_reason||'—')}</td></tr>`}).join('');let latest=d.latest_catalyst_discovery||{};return `<section class="panel wide"><h2>Exa + DeepSeek 独立催化发现</h2><p>影子策略 · 候选 ${esc(latest.candidate_count??0)} · 深度决策 ${esc((latest.decisions||[]).length)} · 创建订单 ${esc(latest.paper_orders_created??0)}</p><table><thead><tr><th>时间</th><th>标的</th><th>工具</th><th>最终动作</th><th>排名</th><th>论点</th><th>风控结论</th></tr></thead><tbody>${rows||'<tr><td colspan="7">尚无催化策略决策</td></tr>'}</tbody></table></section>`}
function render(d){document.getElementById('updated').textContent='刷新 '+new Date().toLocaleTimeString();let h=d.heartbeat||{};document.getElementById('app').innerHTML=`<div class="grid"><div class="panel"><h2>运行状态</h2><div class="metric ${h.status==='ok'?'ok':'warn'}">${esc(h.status||'未知')}</div><p>${esc(h.last_heartbeat_at||'没有心跳')}</p><p>${esc((h.payload||{}).event||'—')}</p></div>${portfolio(d)}${safety(d)}${cycle(d)}</div><div class="grid">${catalyst(d)}${candidates(d)}${optionLine(d)}${orders(d)}${shadow(d)}</div>`}
async function refresh(){try{render(await fetch('/api/state',{cache:'no-store'}).then(r=>r.json()))}catch(e){document.getElementById('app').textContent='读取本地状态失败：'+e}}refresh();setInterval(refresh,5000);
</script></body></html>"""


_CLEAN_PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Paper Trading Control Room</title>
<style>
:root{color-scheme:dark;--bg:#101318;--surface:#191e25;--line:#323a45;--text:#edf1f5;--muted:#aab3bd;--good:#58d6a5;--warn:#f0bd5b;--bad:#ef767a;--info:#71a7f5}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 "Segoe UI",Arial,sans-serif;letter-spacing:0}
header{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:18px 22px;border-bottom:1px solid var(--line)}
h1{font-size:21px;margin:0}h2{font-size:15px;margin:0 0 10px}p{margin:5px 0;color:var(--muted)}
main{padding:18px 22px}.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-bottom:12px}
.panel{background:var(--surface);border:1px solid var(--line);border-radius:6px;padding:14px;overflow:auto}.wide{grid-column:1/-1}
.metric{font-size:22px;font-weight:650}.good{color:var(--good)}.warn{color:var(--warn)}.bad{color:var(--bad)}.info{color:var(--info)}
.badge{display:inline-block;border:1px solid var(--line);border-radius:4px;padding:2px 6px;margin:2px 4px 2px 0}
table{width:100%;border-collapse:collapse;min-width:760px}th,td{padding:8px;text-align:left;vertical-align:top;border-bottom:1px solid var(--line)}
th{color:var(--muted);font-weight:600}.small{font-size:12px;color:var(--muted)}ul{margin:4px 0;padding-left:17px}
@media(max-width:1000px){.grid{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:620px){header{align-items:flex-start;flex-direction:column}main{padding:12px}.grid{grid-template-columns:1fr}.wide{grid-column:auto}}
</style>
</head>
<body>
<header><div><h1>Paper Trading Control Room</h1><p>本地纸面账户与只读市场数据</p></div><div id="updated" class="small"></div></header>
<main id="app"></main>
<script>
const e=v=>String(v??"—").replace(/[&<>'"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[c]));
const money=v=>v===undefined||v===null?"—":"$"+Number(v).toFixed(2);
const pct=v=>v===undefined||v===null?"—":Number(v).toFixed(2)+"%";
const reasons=v=>Array.isArray(v)&&v.length?"<ul>"+v.map(x=>"<li>"+e(x)+"</li>").join("")+"</ul>":"—";
const statusClass=v=>v==="ok"||v==="completed"||v==="filled"||v==="buy"||v==="buy_to_open"?"good":v==="stale"||v==="failed"||v==="timed_out"||v==="rejected"?"bad":"warn";
function health(d){const h=d.heartbeat||{},s=h.effective_status||"unknown",p=h.payload||{};return `<section class="panel"><h2>运行状态</h2><div class="metric ${statusClass(s)}">${e(s)}</div><p>心跳 ${e(h.last_heartbeat_at)}</p><p>年龄 ${e(h.age_seconds)} 秒</p><p>${e(p.event)}</p></section>`}
function account(d){const a=d.account||{},m=d.metrics||{},c=d.daily_counters||{};return `<section class="panel"><h2>共享确定性账户</h2><div class="metric">${money(m.ending_equity)}</div><p>现金 ${money(a.cash)} · 已实现 ${money(a.realized_pnl)}</p><p>今日入场：股票 ${e(c.equity_trades||0)} · 期权 ${e(c.option_trades||0)}</p><p>回撤 ${pct(m.max_drawdown_pct)} · ${e(m.profitability)}</p></section>`}
function weights(d){const w=d.adaptive_weights||{},o=d.outcomes||{},pairs=Object.entries(w.cumulative_squared_loss||{});return `<section class="panel"><h2>加权学习状态</h2><div class="metric info">${e(w.labeled_samples||0)} 个标签</div><p>已解析 ${e(o.resolved_count||0)} · 盈利 ${e(o.profitable_count||0)}</p><p>${pairs.map(([k,v])=>`<span class="badge">${e(k)} ${Number(v).toFixed(3)}</span>`).join("")||"固定权重预热中"}</p></section>`}
function cycle(d){const c=d.latest_cycle||{};return `<section class="panel"><h2>最近主循环</h2><div class="metric">${e(c.event)}</div><p>策略 ${e(c.active_strategy||"weighted_relative_strength_v2")}</p><p>候选 ${e(c.active_candidates||0)} · 入选 ${e((c.selected_candidates||[]).join(", "))}</p><p>股票订单 ${e((c.orders||[]).length)} · 期权订单 ${e((c.option_entries||[]).length)}</p></section>`}
function candidates(d){const rows=(d.candidates||[]).map(x=>`<tr><td>${e(x.ticker)}</td><td class="${statusClass(x.action)}">${e(x.action)}</td><td>${Number(x.score||0).toFixed(3)} / ${Number(x.minimum_entry_score||0).toFixed(3)}</td><td>${e((x.weight_state||{}).mode)}</td><td>${reasons(x.reasons)}</td></tr>`).join("");return `<section class="panel wide"><h2>股票加权策略</h2><table><thead><tr><th>标的</th><th>动作</th><th>分数 / 门槛</th><th>权重模式</th><th>结论</th></tr></thead><tbody>${rows||'<tr><td colspan="5">暂无决策</td></tr>'}</tbody></table></section>`}
function options(d){const line=((d.metrics||{}).lines||{}).options||{},rows=(d.option_decisions||[]).map(x=>`<tr><td>${e(x.ticker)}</td><td>${e(x.option_type)}</td><td class="${statusClass(x.action)}">${e(x.action)}</td><td>${Number(x.call_score||0).toFixed(3)}</td><td>${Number(x.put_score||0).toFixed(3)}</td><td>${reasons(x.reasons)}</td></tr>`).join("");return `<section class="panel wide"><h2>长权利金期权线</h2><p>PnL ${money(line.net_pnl)} · 平仓 ${e(line.closed_trade_count||0)} · 胜率 ${pct(Number(line.win_rate||0)*100)}</p><table><thead><tr><th>标的</th><th>方向</th><th>动作</th><th>Call 分数</th><th>Put 分数</th><th>原因</th></tr></thead><tbody>${rows||'<tr><td colspan="6">暂无决策</td></tr>'}</tbody></table></section>`}
function ai(d){const a=d.ai_gated||{},m=a.metrics||{},rows=(a.decisions||[]).slice().reverse().map(x=>{const z=x.decision||{},r=x.ranking||{},q=x.execution||{};return `<tr><td>${e(x.asof)}</td><td>${e(x.ticker)}</td><td>${Number(r.score||0).toFixed(3)}</td><td>${e(z.instrument)}</td><td class="${statusClass(q.status||z.action)}">${e(q.status||z.action)}</td><td>${Number(z.confidence||0).toFixed(3)}</td><td>${e(z.thesis||z.no_trade_reason)}</td></tr>`}).join("");return `<section class="panel wide"><h2>AI Gated 独立 Paper Sleeve</h2><p>净值 ${money(m.ending_equity)} · 收益 ${pct(m.net_return_pct)} · 平仓 ${e(m.closed_trade_count||0)} · API 与订单状态和主账户隔离</p><table><thead><tr><th>时间</th><th>标的</th><th>排名</th><th>工具</th><th>执行</th><th>置信度</th><th>论点</th></tr></thead><tbody>${rows||'<tr><td colspan="7">暂无 AI 决策</td></tr>'}</tbody></table></section>`}
function render(d){document.getElementById("updated").textContent="刷新 "+new Date().toLocaleTimeString();document.getElementById("app").innerHTML=`<div class="grid">${health(d)}${account(d)}${weights(d)}${cycle(d)}</div><div class="grid">${candidates(d)}${options(d)}${ai(d)}</div>`}
async function refresh(){try{const r=await fetch("/api/state",{cache:"no-store"});render(await r.json())}catch(err){document.getElementById("app").innerHTML=`<section class="panel bad">读取状态失败：${e(err)}</section>`}}
refresh();setInterval(refresh,5000);
</script>
</body>
</html>"""


_BEGINNER_PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>模拟交易日报</title>
<style>
:root{
  color-scheme:light;
  --page:#f4f5f2;--surface:#ffffff;--surface-soft:#f8f9f7;
  --text:#17201b;--muted:#66716a;--line:#d9ded9;
  --good:#137a50;--good-bg:#e8f5ee;--bad:#b73b43;--bad-bg:#fbecee;
  --warn:#8a5a05;--warn-bg:#fff5d9;--info:#245f99;--info-bg:#eaf2fa;
  --neutral:#4c5750;
}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--text);font:14px/1.5 "Segoe UI","Microsoft YaHei",Arial,sans-serif;letter-spacing:0}
header{background:var(--surface);border-bottom:1px solid var(--line)}
.header-inner{max-width:1320px;margin:0 auto;padding:18px 24px;display:flex;align-items:center;justify-content:space-between;gap:18px}
h1{font-size:24px;line-height:1.2;margin:0 0 4px;font-weight:700}
h2{font-size:17px;line-height:1.3;margin:0}
h3{font-size:14px;line-height:1.3;margin:0}
p{margin:4px 0}
.muted{color:var(--muted)}.small{font-size:12px}.strong{font-weight:700}
.status{display:inline-flex;align-items:center;gap:7px;font-weight:650;white-space:nowrap}
.dot{width:9px;height:9px;border-radius:50%;background:var(--neutral);flex:none}
.status.good .dot{background:var(--good)}.status.bad .dot{background:var(--bad)}.status.warn .dot{background:#c28311}
.safety{background:var(--good-bg);border-bottom:1px solid #cde6d7;color:#285940}
.safety-inner{max-width:1320px;margin:0 auto;padding:9px 24px;display:flex;gap:18px;align-items:center;justify-content:space-between}
main{max-width:1320px;margin:0 auto;padding:20px 24px 40px}
.band{background:var(--surface);border:1px solid var(--line);border-radius:6px;margin-bottom:14px;overflow:hidden}
.band-head{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;padding:16px 18px;border-bottom:1px solid var(--line)}
.band-body{padding:16px 18px}
.day-result{display:grid;grid-template-columns:minmax(220px,.72fr) minmax(0,1.8fr);gap:20px;align-items:center}
.pnl-label{font-size:13px;color:var(--muted);margin-bottom:2px}
.pnl{font-size:38px;line-height:1.1;font-weight:750;font-variant-numeric:tabular-nums}
.good-text{color:var(--good)}.bad-text{color:var(--bad)}.warn-text{color:var(--warn)}
.plain-summary{font-size:17px;line-height:1.45;font-weight:650;max-width:780px}
.metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));border-top:1px solid var(--line);margin-top:16px}
.metric{padding:14px 16px;border-right:1px solid var(--line);min-width:0}
.metric:last-child{border-right:0}.metric-name{font-size:12px;color:var(--muted);margin-bottom:3px}
.metric-value{font-size:20px;font-weight:700;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
.metric-note{font-size:12px;color:var(--muted);margin-top:2px}
.progress-pair{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:10px}
.progress-track{height:7px;background:#e6e9e5;border-radius:4px;overflow:hidden;margin-top:5px}
.progress-fill{height:100%;background:var(--info)}
.notice{display:grid;grid-template-columns:18px minmax(0,1fr);gap:10px;padding:12px 0;border-bottom:1px solid var(--line)}
.notice:last-child{border-bottom:0}.notice-mark{width:10px;height:10px;border-radius:2px;margin-top:5px;background:var(--warn)}
.notice.error .notice-mark{background:var(--bad)}.notice-title{font-weight:700}.notice-fix{color:var(--muted);margin-top:2px}
.empty-good{padding:2px 0;color:var(--good);font-weight:650}
.strategy-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}
.strategy{border:1px solid var(--line);border-radius:6px;padding:14px;background:var(--surface-soft);min-width:0}
.strategy-top{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:10px}
.pill{display:inline-flex;align-items:center;border-radius:4px;padding:3px 7px;font-size:12px;font-weight:650;background:#ecefeb;color:var(--neutral);white-space:nowrap}
.pill.good{background:var(--good-bg);color:var(--good)}.pill.bad{background:var(--bad-bg);color:var(--bad)}.pill.warn{background:var(--warn-bg);color:var(--warn)}
.strategy-number{font-size:22px;font-weight:700;font-variant-numeric:tabular-nums}
.strategy-copy{color:var(--muted);min-height:42px}
.table-wrap{overflow:auto}
table{width:100%;border-collapse:collapse;min-width:760px}
th,td{padding:10px 9px;text-align:left;vertical-align:top;border-bottom:1px solid var(--line)}
th{font-size:12px;color:var(--muted);font-weight:650;background:var(--surface-soft)}
tbody tr:last-child td{border-bottom:0}.num{font-variant-numeric:tabular-nums;white-space:nowrap}
.score{min-width:150px}.score-line{display:flex;justify-content:space-between;gap:8px}
.score-track{height:5px;background:#e5e9e5;border-radius:3px;overflow:hidden;margin-top:5px}
.score-fill{height:100%;background:var(--info)}
.reason{max-width:520px;color:var(--muted)}
details{border-top:1px solid var(--line)}details:first-child{border-top:0}
summary{cursor:pointer;padding:13px 0;font-weight:650;list-style-position:outside}
.details-body{padding:0 0 14px;color:var(--muted)}
.advanced-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px 28px}
.kv{display:grid;grid-template-columns:minmax(130px,.6fr) 1fr;gap:8px;padding:5px 0;border-bottom:1px solid #edf0ec}
.kv:last-child{border-bottom:0}.kv span:first-child{color:var(--muted)}
.glossary{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}
.term{border-left:3px solid var(--info);padding-left:10px}.term b{display:block;margin-bottom:2px}
.error-box{background:var(--bad-bg);color:var(--bad);border:1px solid #efc7ca;border-radius:6px;padding:14px}
@media(max-width:900px){
  .day-result{grid-template-columns:1fr}.metrics{grid-template-columns:repeat(2,minmax(0,1fr))}
  .metric:nth-child(2){border-right:0}.metric:nth-child(-n+2){border-bottom:1px solid var(--line)}
  .strategy-grid{grid-template-columns:1fr}.advanced-grid,.glossary{grid-template-columns:1fr}
}
@media(max-width:600px){
  .header-inner,.safety-inner{padding-left:14px;padding-right:14px;align-items:flex-start;flex-direction:column;gap:7px}
  main{padding:14px}.band-head,.band-body{padding:14px}.pnl{font-size:32px}.plain-summary{font-size:15px}
  .metrics{grid-template-columns:1fr}.metric{border-right:0;border-bottom:1px solid var(--line)}
  .metric:nth-child(-n+3){border-bottom:1px solid var(--line)}.metric:last-child{border-bottom:0}
  .progress-pair{grid-template-columns:1fr}.kv{grid-template-columns:1fr;gap:1px}
  table.mobile-stack{min-width:0}
  table.mobile-stack thead{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
  table.mobile-stack,table.mobile-stack tbody,table.mobile-stack tr,table.mobile-stack td{display:block;width:100%}
  table.mobile-stack tr{padding:9px 0;border-bottom:1px solid var(--line)}
  table.mobile-stack tr:last-child{border-bottom:0}
  table.mobile-stack td{display:grid;grid-template-columns:88px minmax(0,1fr);gap:8px;padding:5px 14px;border:0;white-space:normal}
  table.mobile-stack td::before{content:attr(data-label);color:var(--muted);font-size:12px;font-weight:650}
  table.mobile-stack .score{min-width:0}
}
</style>
</head>
<body>
<header>
  <div class="header-inner">
    <div><h1>模拟交易日报</h1><p class="muted">先看结果，再看原因；技术细节放在页面底部。</p></div>
    <div><div id="service-status" class="status"><span class="dot"></span><span>读取中</span></div><div id="updated" class="small muted"></div></div>
  </div>
</header>
<div class="safety"><div class="safety-inner"><strong>仅使用假钱模拟，不会动用 Robinhood 现金</strong><span>行情只读 · 本地下单 · 无真实交易入口</span></div></div>
<main id="app"><section class="band"><div class="band-body">正在读取本地状态...</div></section></main>
<script>
const esc=v=>String(v??"—").replace(/[&<>'"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[c]));
const number=v=>Number(v||0);
const money=v=>"$"+Math.abs(number(v)).toFixed(2);
const signedMoney=v=>(number(v)>0?"+":number(v)<0?"-":"")+money(v);
const pct=v=>number(v).toFixed(2)+"%";
const localTime=v=>{if(!v)return "—";const d=new Date(v);return Number.isNaN(d.getTime())?"—":d.toLocaleTimeString("zh-CN",{hour:"2-digit",minute:"2-digit",hour12:false})};
const tone=v=>number(v)>0?"good-text":number(v)<0?"bad-text":"";
const serviceLabel={ok:"运行中",stale:"心跳已过期",stopped:"已停止",unknown:"状态未知"};
const sessionLabel={pre_market:"盘前监控",regular:"正常交易时段",post_market:"盘后监控",closed:"休市"};
const statusLabel={
  traded:"已交易",observed:"仅观察",validation_error:"报价校验故障",
  failed_closed:"出错后安全停止",completed:"已完成",no_trade:"暂不交易",
  buy:"准备买入",buy_to_open:"准备买入期权",filled:"已模拟成交",
  rejected:"已拒绝",open:"等待成交",cancelled:"已取消",expired:"已过期"
};
function humanReason(value){
  const x=String(value||"");
  let m;
  if(!x)return "没有记录原因";
  if(x.includes("binary earnings event inside equity exclusion window"))return "临近财报，股票价格可能剧烈波动，系统不在此时新开仓";
  if(x.includes("binary earnings event inside exclusion window"))return "临近财报，期权波动和时间价值风险过高";
  if(x.includes("existing position is managed by the exit pipeline"))return "当时已经持有该股票，交由止盈止损模块管理";
  if((m=x.match(/weighted technical score ([0-9.]+) below ([0-9.]+)/)))return `综合分 ${m[1]}，低于入场线 ${m[2]}`;
  if((m=x.match(/best weighted option score below ([0-9.]+)/)))return `期权方向分数低于入场线 ${m[1]}`;
  if(x.includes("future option quote would create lookahead"))return "期权报价时间校验失败（已定位并修复）";
  if(x.includes("option spread too wide"))return "买卖价差太大，模拟成交成本过高";
  if(x.includes("insufficient option volume"))return "该期权成交量不足";
  if(x.includes("insufficient option open interest"))return "该期权未平仓量不足";
  if(x.includes("directional option threshold not met"))return "期权方向不够明确";
  if(x.includes("outside regular market session"))return "不在美股正常交易时段";
  if(x.includes("completed OHLCV history is stale"))return "历史行情没有更新，系统拒绝使用旧数据";
  if(x.includes("extreme chase risk"))return "涨幅过急，追高风险过大";
  if(x.includes("no contract passed filters"))return "没有期权合约同时通过价格、流动性和时间校验";
  if(x.includes("mandatory pre-close flatten"))return "按规则在收盘前平仓，不留隔夜仓位";
  if(x.includes("overnight recovery flatten"))return "服务恢复后立即平掉不应隔夜的仓位";
  return x;
}
function serviceHeader(b){
  const s=(b.service||{}).status||"unknown";
  const node=document.getElementById("service-status");
  node.className="status "+(s==="ok"?"good":s==="stale"||s==="stopped"?"bad":"warn");
  node.innerHTML=`<span class="dot"></span><span>${esc(serviceLabel[s]||s)}</span>`;
}
function overview(d){
  const b=d.beginner_summary||{},day=b.day||{},a=b.account||{},ev=b.evidence||{},svc=b.service||{};
  const trades=day.trades||[];
  let summary;
  if(day.closed_trades){
    const names=trades.map(x=>x.symbol).filter(Boolean).join("、");
    summary=`${esc(b.session_date)} 完成 ${day.closed_trades} 笔交易（${esc(names)}），合计${day.realized_pnl>=0?"盈利":"亏损"} ${money(day.realized_pnl)}，目前没有把这一天的结果当成策略有效证明。`;
  }else{
    summary=`${esc(b.session_date)} 没有完成交易。系统可能没有找到足够好的机会，或某条策略因数据/模型问题安全停止。`;
  }
  const sp=Math.min(100,number(ev.forward_sessions)/Math.max(1,number(ev.minimum_forward_sessions))*100);
  const tp=Math.min(100,number(ev.closed_trades)/Math.max(1,number(ev.minimum_closed_trades))*100);
  return `<section class="band">
    <div class="band-body">
      <div class="day-result">
        <div><div class="pnl-label">最近交易日实际已赚 / 已亏（假钱）</div><div class="pnl ${tone(day.realized_pnl)}">${signedMoney(day.realized_pnl)}</div><div class="muted">${day.wins||0} 盈 · ${day.losses||0} 亏 · ${day.closed_trades||0} 笔已平仓</div></div>
        <div><div class="plain-summary">${summary}</div><p class="muted">当前市场：${esc(sessionLabel[svc.market_session]||svc.market_session||"未识别")}。当前持仓 ${number(a.open_equity_positions)+number(a.open_option_positions)} 个。</p></div>
      </div>
      <div class="metrics">
        <div class="metric"><div class="metric-name">模拟账户总资产</div><div class="metric-value">${money(a.ending_equity)}</div><div class="metric-note">初始假钱 ${money(a.initial_cash)}</div></div>
        <div class="metric"><div class="metric-name">启用以来累计结果</div><div class="metric-value ${tone(a.cumulative_pnl)}">${signedMoney(a.cumulative_pnl)}</div><div class="metric-note">累计收益率 ${pct(a.cumulative_return_pct)}</div></div>
        <div class="metric"><div class="metric-name">最大历史回撤</div><div class="metric-value">${pct(ev.max_drawdown_pct)}</div><div class="metric-note">账户从阶段高点最多跌多少</div></div>
        <div class="metric"><div class="metric-name">盈利判断</div><div class="metric-value warn-text">${ev.sufficient?"样本达到最低线":"样本还不够"}</div><div class="metric-note">现在不能判断能否稳定盈利</div></div>
      </div>
      <div class="progress-pair">
        <div><div class="small muted">前向模拟天数 ${ev.forward_sessions||0} / ${ev.minimum_forward_sessions||20}</div><div class="progress-track"><div class="progress-fill" style="width:${sp}%"></div></div></div>
        <div><div class="small muted">已平仓交易 ${ev.closed_trades||0} / ${ev.minimum_closed_trades||30}</div><div class="progress-track"><div class="progress-fill" style="width:${tp}%"></div></div></div>
      </div>
    </div>
  </section>`;
}
function issues(d){
  const list=(d.beginner_summary||{}).issues||[];
  return `<section class="band">
    <div class="band-head"><div><h2>需要关注</h2><p class="muted">这里区分“正常没有机会”和“系统出错所以没交易”。</p></div><span class="pill ${list.length?"bad":"good"}">${list.length?list.length+" 类问题":"没有发现故障"}</span></div>
    <div class="band-body">${list.length?list.map(x=>`<div class="notice ${x.severity==="error"?"error":""}"><span class="notice-mark"></span><div><div class="notice-title">${esc(x.title)} <span class="muted">(${x.count} 次)</span></div><div>${esc(x.impact)}</div><div class="notice-fix">处理：${esc(x.resolution)}</div></div></div>`).join(""):'<div class="empty-good">最近交易日没有发现需要处理的运行故障。</div>'}</div>
  </section>`;
}
function tradeTable(d){
  const b=d.beginner_summary||{},rows=((b.day||{}).trades||[]).map(x=>`<tr>
    <td data-label="标的"><strong>${esc(x.symbol)}</strong><div class="small muted">${esc(x.instrument==="equity"?"股票":"期权")}</div></td>
    <td data-label="数量" class="num">${esc(x.quantity)}</td><td data-label="买入" class="num">${money(x.entry_price)}<div class="small muted">${localTime(x.entry_time)} 买入</div></td>
    <td data-label="卖出" class="num">${money(x.exit_price)}<div class="small muted">${localTime(x.exit_time)} 卖出</div></td>
    <td data-label="结果" class="num ${tone(x.realized_pnl)}"><strong>${signedMoney(x.realized_pnl)}</strong><div class="small">${number(x.return_pct).toFixed(2)}%</div></td>
    <td data-label="卖出原因" class="reason">${humanReason(x.exit_reason||"已平仓")}<div class="small muted">持有 ${number(x.holding_minutes).toFixed(0)} 分钟</div></td>
  </tr>`).join("");
  return `<section class="band"><div class="band-head"><div><h2>${esc(b.session_date)} 做了什么</h2><p class="muted">只统计已完成的买入和卖出；创建订单不等于已经持仓。</p></div></div><div class="table-wrap"><table class="mobile-stack"><thead><tr><th>标的</th><th>数量</th><th>买入</th><th>卖出</th><th>结果</th><th>为什么卖出</th></tr></thead><tbody>${rows||'<tr><td colspan="6" class="muted">这一天没有已平仓交易。</td></tr>'}</tbody></table></div></section>`;
}
function strategies(d){
  const b=d.beginner_summary||{},s=b.strategy_lines||{},eq=s.equity||{},op=s.options||{},ai=s.ai||{};
  const equityShadow=((d.strategy_modes||{}).weighted_relative_strength_v2||"")==="shadow_only";
  const eqCopy=equityShadow?`监控 ${eq.watchlist_count||0} 个标的；当前只保存候选和未来收益标签，不会创建新的股票订单。`:eq.entries?`监控 ${eq.watchlist_count||0} 个标的；开仓 ${eq.entries||0} 笔，平仓 ${eq.closed_trades||0} 笔。${eq.earnings_risk_entries?`其中 ${eq.earnings_risk_entries} 笔发生在临近财报的标的，已新增财报风险门。`:"系统会继续按加权分数和确定性风控筛选。"}`:`监控 ${eq.watchlist_count||0} 个标的；这一天没有新开股票仓位。`;
  const opCopy=op.status==="validation_error"?`完成 ${op.direction_evaluations||0} 次方向评估；进入合约筛选 ${op.selection_attempts||0} 次，但报价时间校验故障使合约全部落选。`:`完成 ${op.direction_evaluations||0} 次方向评估；进入合约筛选 ${op.selection_attempts||0} 次，未创建模拟期权订单。`;
  const directional=(((d.ai_gated||{}).metrics||{}).directional_breakdown)||{},bull=directional.bullish||{},bear=directional.bearish||{};
  const directionCopy=(bull.proposal_count||bear.proposal_count)?` 看涨提案 ${bull.proposal_count||0} 个、净结果 ${signedMoney(bull.net_pnl)}；看跌提案 ${bear.proposal_count||0} 个、净结果 ${signedMoney(bear.net_pnl)}。`:"";
  const aiCopy=(ai.status==="failed_closed"?`${ai.failed||0} 次模型排名失败，系统安全停止，没有下单。`:`完成 ${ai.completed||0} 次 AI 决策循环；最近一次从 ${ai.latest_candidate_count||0} 个候选中选出 ${ai.latest_top_set_count||0} 个做新闻研究。`)+directionCopy;
  return `<section class="band"><div class="band-head"><div><h2>三条策略线分别发生了什么</h2><p class="muted">股票和期权共享主模拟账户；AI 使用独立模拟账户，三条线分别统计。</p></div></div><div class="band-body"><div class="strategy-grid">
    <article class="strategy"><div class="strategy-top"><h3>股票加权策略</h3><span class="pill ${equityShadow?"good":eq.status==="traded"?"good":""}">${equityShadow?"只观察":esc(statusLabel[eq.status]||eq.status)}</span></div><div class="strategy-number ${tone(eq.daily_pnl)}">${signedMoney(eq.daily_pnl)}</div><p class="strategy-copy">${esc(eqCopy)}</p></article>
    <article class="strategy"><div class="strategy-top"><h3>买入 Call / Put 期权</h3><span class="pill ${op.status==="validation_error"?"bad":""}">${esc(statusLabel[op.status]||op.status)}</span></div><div class="strategy-number">${op.orders||0} 笔订单</div><p class="strategy-copy">${esc(opCopy)}</p></article>
    <article class="strategy"><div class="strategy-top"><h3>AI 独立模拟策略</h3><span class="pill ${ai.status==="failed_closed"?"bad":"good"}">${esc(statusLabel[ai.status]||ai.status)}</span></div><div class="strategy-number">${ai.completed||0} 次完成</div><p class="strategy-copy">${esc(aiCopy)}</p></article>
  </div></div></section>`;
}
function allocator(d){
  const a=d.ai_instrument_allocator||{},m=a.metrics||{},allocation=a.latest_allocation||{},selected=allocation.selected_instrument||{},cf=allocation.counterfactual_2000||{},short=a.short_equity_counterfactual||{},mandates=a.mandates||[],plans=a.plans||[],costs=m.execution_cost_decomposition||{};
  const stageLabel={overnight:"晚间完整研究",premarket_update:"盘前更新",preopen_revalidation:"开盘前复核",open_execution:"开盘后执行复核",intraday:"盘中研究"};
  const horizonLabel={intraday_close:"当天收盘前",next_close:"下一交易日收盘前",two_to_five_days:"持有 2 至 5 个交易日"};
  const instrument=selected.instrument_type==="equity"?"股票":selected.instrument_type==="call"?"看涨 Call":selected.instrument_type==="put"?"看跌 Put":"尚未选中";
  const affordability=allocation.counterfactual_2000==null?"尚无可比较的已选工具":cf.affordable?`同一工具可负担，最多 ${esc(cf.max_affordable_quantity)} 单位`:`同一工具不可负担：${esc(cf.rejection_reason||"超过风险预算")}`;
  const rows=(a.decisions||[]).slice().reverse().slice(0,6).map(x=>{const s=x.signal||{};return `<tr><td>${localTime(x.asof)}</td><td><strong>${esc(x.ticker)}</strong></td><td>${esc(stageLabel[x.stage]||x.stage||"—")}</td><td>${esc(horizonLabel[s.horizon]||s.horizon||"—")}</td><td>${esc(s.action||"—")}</td><td class="reason">${esc(s.thesis||s.no_trade_reason||"—")}</td></tr>`}).join("");
  return `<section class="band"><div class="band-head"><div><h2>AI 股票/期权选择器 · 独立 $10,000 模拟账户</h2><p class="muted">先判断指定持有期内可能落入哪个涨跌区间，再由 Python 比较股票、Call 或 Put；模型不能直接下单。</p><p class="small muted">策略编号：ai_instrument_allocator_v1</p></div><span class="pill good">独立纸面账户</span></div><div class="band-body">
    <div class="metrics">
      <div class="metric"><div class="metric-name">当前净值</div><div class="metric-value">${m.ending_equity==null?"尚未初始化":money(m.ending_equity)}</div><div class="metric-note">初始假钱 $10,000</div></div>
      <div class="metric"><div class="metric-name">累计结果</div><div class="metric-value ${tone(m.realized_pnl)}">${m.realized_pnl==null?"—":signedMoney(m.realized_pnl)}</div><div class="metric-note">独立于旧 $2,000 账本</div></div>
      <div class="metric"><div class="metric-name">最近选中的工具</div><div class="metric-value">${esc(selected.ticker||"—")} ${esc(instrument)}</div><div class="metric-note">${selected.quantity==null?"没有订单":`${esc(selected.quantity)} 单位，计划价 ${money(selected.entry_price)}`}</div></div>
      <div class="metric"><div class="metric-name">执行成本核对</div><div class="metric-value">${costs.closed_round_trip_count||0} 笔闭环</div><div class="metric-note">恒等式残差 ${number(costs.identity_residual_usd).toFixed(6)}</div></div>
    </div>
    <div class="advanced-grid" style="margin-top:14px">
      <div><h3>$2,000 可负担性对照</h3><p>${affordability}</p><p class="small muted">只检查 $10,000 分配器已经选中的同一工具，不会重选股票、行权价或到期日。</p></div>
      <div><h3>假设直接做空的对照（不交易）</h3><p>${short.benchmark_name?`${esc(short.ticker)} 的假设直接做空结果只单独记录。`:"最近没有看跌对照记录。"}</p><p class="small muted">不会进入账户、不会创建订单，也不会与买入 Put 的盈亏合并。</p></div>
      <div><h3>持仓期限与重启恢复</h3><p>${mandates.filter(x=>x.status==="open").length} 个有效持仓计划 · ${plans.filter(x=>x.status==="active").length} 个待执行计划</p><p class="small muted">可选择当天收盘前、下一交易日收盘前，或持有 2 至 5 个交易日。持仓计划缺失时会安全平仓。</p></div>
      <div><h3>模型概率目前怎么用</h3><p>${allocation.probability_ev_available?"校准完成后可显示按概率估算的预期收益":"尚未完成概率校准，不显示按概率估算的预期收益"}</p><p class="small muted">当前只用各涨跌区间的原始概率做候选排序和保守情景比较。</p></div>
    </div>
    <div class="table-wrap" style="margin-top:14px"><table><thead><tr><th>时间</th><th>股票</th><th>阶段</th><th>期限</th><th>结论</th><th>简要论点</th></tr></thead><tbody>${rows||'<tr><td colspan="6" class="muted">尚无 allocator 模型决策。</td></tr>'}</tbody></table></div>
  </div></section>`;
}
function newsDrift(d){
  const lane=d.news_drift||{},m=lane.metrics||{},c=lane.latest_cycle||{},h=(m.horizons||{}).next_close||{},p=h.portfolio_day||{};
  const signals=(c.signals||[]).slice(0,8);
  const rows=signals.map(x=>`<tr><td><strong>${esc(x.ticker||"未映射")}</strong></td><td>${esc(x.direction||"不明确")}</td><td>${esc(x.event_type||"其他")}</td><td>${(number(x.materiality)*100).toFixed(1)}%</td><td class="reason">${esc(x.rationale||"—")}</td></tr>`).join("");
  return `<section class="band"><div class="band-head"><div><h2>新闻漂移影子实验</h2><p class="muted">Exa 先发现全市场新闻，DeepSeek 只看标题做映射；随后才检查价格和流动性。仅记录模拟提案，不会创建股票或期权订单。</p></div><span class="pill good">只观察</span></div><div class="band-body"><div class="metrics"><div class="metric"><div class="metric-name">已保存事件</div><div class="metric-value">${m.event_count||0}</div></div><div class="metric"><div class="metric-name">影子提案</div><div class="metric-value">${m.proposal_count||0}</div></div><div class="metric"><div class="metric-name">有效收益标签</div><div class="metric-value">${m.valid_return_label_count||0}</div></div><div class="metric"><div class="metric-name">次日收盘净收益</div><div class="metric-value ${tone(p.mean_return_pct)}">${p.mean_return_pct==null?"样本不足":pct(p.mean_return_pct)}</div><div class="metric-note">portfolio-day 胜率 ${p.hit_rate==null?"—":pct(number(p.hit_rate)*100)}</div></div></div><div class="table-wrap"><table><thead><tr><th>股票</th><th>方向</th><th>事件</th><th>重要性</th><th>模型依据</th></tr></thead><tbody>${rows||'<tr><td colspan="5" class="muted">尚无新的新闻信号；重复新闻不会再次发送给模型。</td></tr>'}</tbody></table></div><p class="small muted">当前判断：${esc(m.profitability||"insufficient_forward_evidence")}。Exa 单次检索价格未配置时会显示为未计价，不会当作零成本。</p></div></section>`;
}
function candidates(d){
  const values=(d.candidates||[]).slice().sort((a,b)=>number(b.score)-number(a.score)).slice(0,8);
  const rows=values.map(x=>{
    const score=number(x.score)*100,threshold=number(x.minimum_entry_score)*100;
    const why=(x.reasons||[]).map(humanReason).join("；")||"综合分达到入场线";
    return `<tr><td data-label="股票"><strong>${esc(x.ticker)}</strong><div class="small muted">${localTime(x.asof)} 最后评估</div></td><td data-label="动作"><span class="pill ${x.action==="buy"?"good":"warn"}">${esc(statusLabel[x.action]||x.action)}</span></td><td data-label="综合分" class="score"><div class="score-line"><span>${score.toFixed(1)} 分</span><span class="small muted">入场线 ${threshold.toFixed(1)}</span></div><div class="score-track"><div class="score-fill" style="width:${Math.min(100,score)}%"></div></div></td><td data-label="原因" class="reason">${esc(why)}</td></tr>`;
  }).join("");
  return `<section class="band"><div class="band-head"><div><h2>最后一次股票筛选</h2><p class="muted">这里只显示分数最高的 8 个。当前股票线只记录候选和 360 分钟后的模拟结果，不会因为分数高而创建订单。</p></div></div><div class="table-wrap"><table class="mobile-stack"><thead><tr><th>股票</th><th>系统动作</th><th>综合分</th><th>简单原因</th></tr></thead><tbody>${rows||'<tr><td colspan="4" class="muted">暂无股票筛选记录。</td></tr>'}</tbody></table></div></section>`;
}
function advanced(d){
  const b=d.beginner_summary||{},op=((b.strategy_lines||{}).options||{}),ops=b.operations||{},m=d.metrics||{},w=d.adaptive_weights||{};
  const optionRows=(d.option_decisions||[]).slice().sort((a,b)=>number(b.score)-number(a.score)).slice(0,8).map(x=>`<tr><td>${esc(x.ticker)}</td><td>${esc(x.option_type==="put"?"看跌 Put":x.option_type==="call"?"看涨 Call":"不选方向")}</td><td>${number(Math.max(x.call_score||0,x.put_score||0)*100).toFixed(1)}</td><td class="reason">${esc((x.reasons||[]).map(humanReason).join("；"))}</td></tr>`).join("");
  return `<section class="band"><div class="band-body">
    <details><summary>高级运行详情</summary><div class="details-body"><div class="advanced-grid">
      <div><h3>运行与成本</h3><div class="kv"><span>受监督作业</span><span>${ops.runtime_jobs||0} 个，失败 ${ops.failed_jobs||0}</span></div><div class="kv"><span>模型调用</span><span>${ops.llm_calls||0} 次，错误 ${ops.llm_errors||0}</span></div><div class="kv"><span>已记录 API 成本</span><span>$${number(ops.estimated_api_cost_usd).toFixed(4)}${ops.historical_cost_incomplete?"（旧失败调用成本未完整记录）":""}</span></div><div class="kv"><span>真实下单</span><span class="good-text">没有调用</span></div></div>
      <div><h3>累计策略指标</h3><div class="kv"><span>胜率</span><span>${pct(number(m.win_rate)*100)}</span></div><div class="kv"><span>利润因子</span><span>${number(m.profit_factor).toFixed(2)}</span></div><div class="kv"><span>成交率</span><span>${pct(number(m.fill_rate)*100)}</span></div><div class="kv"><span>学习标签</span><span>${esc(w.labeled_samples||0)} 个</span></div></div>
    </div><h3 style="margin-top:16px">最后一次期权方向筛选</h3><div class="table-wrap"><table><thead><tr><th>标的</th><th>方向</th><th>最高分</th><th>未交易原因</th></tr></thead><tbody>${optionRows||'<tr><td colspan="4">暂无记录</td></tr>'}</tbody></table></div><p class="small muted">期权筛选尝试 ${op.selection_attempts||0} 次。这里的分数和 Greeks 仅用于模拟研究，不会触发真实 Robinhood 订单。</p></div></details>
    <details><summary>新手词汇表</summary><div class="details-body"><div class="glossary">
      <div class="term"><b>PnL（盈亏）</b><span>卖出所得减去买入成本和模拟滑点。正数赚钱，负数亏钱。</span></div>
      <div class="term"><b>回撤</b><span>账户从某个阶段高点向下跌了多少。越小通常表示波动风险越低。</span></div>
      <div class="term"><b>胜率</b><span>盈利交易数除以已平仓交易数。胜率高不等于一定赚钱，还要看每次赚亏大小。</span></div>
      <div class="term"><b>利润因子</b><span>总盈利除以总亏损。高于 1 才表示历史总盈利大于总亏损。</span></div>
      <div class="term"><b>Call / Put</b><span>Call 偏向看涨，Put 偏向看跌。本系统只模拟买方，最大合约损失限于已付权利金。</span></div>
      <div class="term"><b>Fail-closed</b><span>数据或模型异常时不猜、不下单，直接安全停止该次决策。</span></div>
    </div></div></details>
  </div></section>`;
}
function render(d){
  const b=d.beginner_summary||{};
  serviceHeader(b);
  document.getElementById("updated").textContent=`数据刷新 ${new Date().toLocaleTimeString("zh-CN",{hour12:false})} · 最近交易日 ${b.session_date||"—"}`;
  document.getElementById("app").innerHTML=overview(d)+issues(d)+tradeTable(d)+strategies(d)+allocator(d)+newsDrift(d)+candidates(d)+advanced(d);
}
async function refresh(){
  try{
    const response=await fetch("/api/state",{cache:"no-store"});
    if(!response.ok)throw new Error("HTTP "+response.status);
    render(await response.json());
  }catch(error){
    document.getElementById("app").innerHTML=`<div class="error-box"><strong>无法读取本地状态</strong><div>${esc(error.message||error)}</div></div>`;
  }
}
refresh();setInterval(refresh,5000);
</script>
</body>
</html>"""


_BEGINNER_PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>模拟交易控制台</title>
<style>
:root{
  color-scheme:light;
  --page:#f3f5f4;--surface:#fff;--surface-soft:#f8faf9;
  --text:#18221d;--muted:#627069;--line:#d9dfdc;--line-strong:#c7d0cc;
  --green:#0b7650;--green-soft:#e7f4ee;--red:#bd3542;--red-soft:#faeaec;
  --amber:#8d5b06;--amber-soft:#fff3d6;--blue:#205f96;--blue-soft:#eaf2f9;
  --neutral:#46534d;
}
*{box-sizing:border-box}
html{background:var(--page)}
body{margin:0;background:var(--page);color:var(--text);font:14px/1.5 "Segoe UI","Microsoft YaHei",Arial,sans-serif;letter-spacing:0}
button{font:inherit;letter-spacing:0}
.topbar{background:var(--surface);border-bottom:1px solid var(--line)}
.topbar-inner{max-width:1420px;margin:0 auto;padding:16px 24px;display:flex;align-items:center;justify-content:space-between;gap:24px}
.brand{min-width:0}.brand h1{font-size:22px;line-height:1.2;margin:0 0 3px}.brand p{margin:0;color:var(--muted);font-size:12px}
.runtime-strip{display:flex;align-items:center;justify-content:flex-end;gap:16px;flex-wrap:wrap}
.runtime-item{display:grid;grid-template-columns:auto auto;align-items:center;gap:7px;white-space:nowrap}
.runtime-label{font-size:11px;color:var(--muted)}.runtime-value{font-weight:700}
.dot{width:8px;height:8px;border-radius:50%;background:var(--neutral)}
.dot.good{background:var(--green)}.dot.bad{background:var(--red)}.dot.warn{background:#c18110}.dot.info{background:var(--blue)}
.paper-boundary{background:var(--green-soft);color:#235b42;border-bottom:1px solid #cce4d7}
.paper-boundary.bad{background:var(--red-soft);color:#8c2532;border-bottom-color:#efc4ca}
.paper-boundary-inner{max-width:1420px;margin:0 auto;padding:8px 24px;display:flex;align-items:center;justify-content:space-between;gap:16px}
.paper-boundary strong{font-size:13px}.paper-boundary span{font-size:12px}
.tabbar{position:sticky;top:0;z-index:20;background:rgba(255,255,255,.97);border-bottom:1px solid var(--line)}
.tabs{max-width:1420px;margin:0 auto;padding:0 24px;display:flex;gap:4px;overflow-x:auto;scrollbar-width:thin}
.tab{height:46px;padding:0 15px;border:0;border-bottom:3px solid transparent;background:transparent;color:var(--muted);font-weight:650;white-space:nowrap;cursor:pointer}
.tab:hover{color:var(--text);background:var(--surface-soft)}
.tab[aria-selected="true"]{color:var(--blue);border-bottom-color:var(--blue)}
.tab:focus-visible{outline:2px solid var(--blue);outline-offset:-3px}
main{max-width:1420px;margin:0 auto;padding:20px 24px 48px}
.view[hidden]{display:none}.view{min-height:480px}
.page-heading{display:flex;align-items:flex-start;justify-content:space-between;gap:18px;margin-bottom:14px}
.page-heading h2{font-size:19px;line-height:1.3;margin:0 0 3px}.page-heading p{margin:0;color:var(--muted)}
.asof{font-size:12px;color:var(--muted);white-space:nowrap;padding-top:4px}
.activity{display:grid;grid-template-columns:minmax(0,1.65fr) minmax(260px,.7fr);gap:14px;margin-bottom:14px}
.activity-main,.current-alerts{background:var(--surface);border:1px solid var(--line);border-radius:6px;padding:17px 18px;min-width:0}
.eyebrow{font-size:11px;color:var(--muted);font-weight:700;text-transform:uppercase;margin-bottom:5px}
.activity-title{font-size:20px;line-height:1.35;font-weight:750;margin-bottom:5px}.activity-copy{color:var(--muted);margin:0}
.status-badge{display:inline-flex;align-items:center;gap:6px;border-radius:4px;padding:3px 7px;font-size:12px;font-weight:700;background:#edf0ee;color:var(--neutral);white-space:nowrap}
.status-badge.good{background:var(--green-soft);color:var(--green)}.status-badge.bad{background:var(--red-soft);color:var(--red)}
.status-badge.warn{background:var(--amber-soft);color:var(--amber)}.status-badge.info{background:var(--blue-soft);color:var(--blue)}
.alert-head{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:10px}.alert-head h3{font-size:14px;margin:0}
.alert-list{display:grid;gap:8px}.alert-row{display:grid;grid-template-columns:9px minmax(0,1fr);gap:8px;align-items:start;color:var(--muted)}
.alert-mark{width:8px;height:8px;border-radius:2px;background:var(--amber);margin-top:6px}.alert-row.bad .alert-mark{background:var(--red)}
.clear-state{color:var(--green);font-weight:650}
.account-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px;margin-bottom:14px}
.account-card{background:var(--surface);border:1px solid var(--line);border-radius:6px;overflow:hidden;min-width:0}
.account-head{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;padding:14px 16px;border-bottom:1px solid var(--line)}
.account-head h3{font-size:15px;margin:0 0 2px}.account-head p{margin:0;color:var(--muted);font-size:12px}
.account-body{display:grid;grid-template-columns:minmax(170px,.85fr) minmax(0,1.4fr);align-items:stretch}
.account-primary{padding:17px 16px;border-right:1px solid var(--line)}
.account-equity{font-size:28px;line-height:1.15;font-weight:760;font-variant-numeric:tabular-nums;margin:3px 0}.account-pnl{font-weight:700;font-variant-numeric:tabular-nums}
.good-text{color:var(--green)}.bad-text{color:var(--red)}.warn-text{color:var(--amber)}
.account-facts{display:grid;grid-template-columns:repeat(2,minmax(0,1fr))}
.fact{padding:12px 14px;border-bottom:1px solid var(--line);min-width:0}.fact:nth-child(odd){border-right:1px solid var(--line)}.fact:nth-last-child(-n+2){border-bottom:0}
.fact-label{font-size:11px;color:var(--muted);margin-bottom:2px}.fact-value{font-size:15px;font-weight:700;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
.section-block{background:var(--surface);border:1px solid var(--line);border-radius:6px;margin-bottom:14px;overflow:hidden}
.section-head{display:flex;align-items:flex-start;justify-content:space-between;gap:14px;padding:14px 16px;border-bottom:1px solid var(--line)}
.section-head h3{font-size:15px;margin:0 0 2px}.section-head p{margin:0;color:var(--muted);font-size:12px}
.table-wrap{overflow:auto}table{width:100%;border-collapse:collapse;min-width:760px}
th,td{padding:10px 12px;text-align:left;vertical-align:top;border-bottom:1px solid var(--line)}th{font-size:11px;color:var(--muted);font-weight:700;background:var(--surface-soft)}
tbody tr:last-child td{border-bottom:0}.num{font-variant-numeric:tabular-nums;white-space:nowrap}.muted{color:var(--muted)}.small{font-size:12px}
.reason{max-width:540px;color:var(--muted);overflow-wrap:anywhere}
.pipeline{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:0;padding:14px 16px}
.opportunity-pipeline{grid-template-columns:repeat(6,minmax(0,1fr))}
.pipeline-step{padding:8px 12px;min-width:0;border-right:1px solid var(--line)}.pipeline-step:last-child{border-right:0}
.pipeline-step strong{display:block;font-size:13px}.pipeline-step span{display:block;color:var(--muted);font-size:11px;margin-top:2px}
.stat-strip{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));border-bottom:1px solid var(--line)}
.stat{padding:12px 14px;border-right:1px solid var(--line);min-width:0}.stat:last-child{border-right:0}.stat-label{font-size:11px;color:var(--muted)}.stat-value{font-size:18px;font-weight:750;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
.issue-list{padding:4px 16px}.issue{display:grid;grid-template-columns:10px minmax(0,1fr) auto;gap:10px;padding:11px 0;border-bottom:1px solid var(--line);align-items:start}.issue:last-child{border-bottom:0}.issue-mark{width:8px;height:8px;border-radius:2px;background:var(--amber);margin-top:6px}.issue.error .issue-mark{background:var(--red)}
.issue-title{font-weight:700}.issue-copy{color:var(--muted);font-size:12px;margin-top:2px}.issue-count{font-variant-numeric:tabular-nums;color:var(--muted);white-space:nowrap}
.definition-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:0;border-top:1px solid var(--line)}.definition{padding:12px 14px;border-right:1px solid var(--line)}.definition:last-child{border-right:0}.definition strong{display:block;margin-bottom:2px}.definition span{font-size:12px;color:var(--muted)}
.evidence-details summary{cursor:pointer;color:var(--text);font-weight:600;list-style-position:outside}.evidence-details div{margin-top:7px;color:var(--muted);white-space:normal}.evidence-details[open] summary{color:var(--blue)}
.history-details>summary{cursor:pointer;padding:14px 16px;font-weight:700}.history-details>summary span{display:block;margin-top:2px;color:var(--muted);font-size:12px;font-weight:400}.history-details[open]>summary{border-bottom:1px solid var(--line);color:var(--blue)}
.empty-state{background:var(--surface);border:1px solid var(--line);border-radius:6px;padding:36px 20px;text-align:center;color:var(--muted)}
.empty-state h2{color:var(--text);font-size:18px;margin:0 0 5px}
.error-box{background:var(--red-soft);border:1px solid #e9bdc2;border-radius:6px;padding:16px;color:var(--red)}
@media(max-width:940px){
  .activity{grid-template-columns:1fr}.account-grid{grid-template-columns:1fr}
  .topbar-inner{align-items:flex-start}.runtime-strip{gap:10px 16px}.pipeline{grid-template-columns:1fr}.pipeline-step{padding:8px 0;border-right:0;border-bottom:1px solid var(--line)}.pipeline-step:last-child{border-bottom:0}.stat-strip{grid-template-columns:repeat(2,minmax(0,1fr))}.stat:nth-child(2){border-right:0}.stat:nth-child(-n+2){border-bottom:1px solid var(--line)}
}
@media(max-width:680px){
  .topbar-inner,.paper-boundary-inner{padding-left:14px;padding-right:14px;align-items:flex-start;flex-direction:column;gap:10px}
  .runtime-strip{justify-content:flex-start}.paper-boundary-inner{gap:3px}.tabs{padding:0 8px}.tab{padding:0 12px}
  main{padding:14px}.page-heading{display:block}.asof{margin-top:4px}.activity-main,.current-alerts{padding:14px}
  .account-body{grid-template-columns:1fr}.account-primary{border-right:0;border-bottom:1px solid var(--line)}
  .account-facts{grid-template-columns:1fr}.fact:nth-child(odd){border-right:0}.fact:nth-last-child(-n+2){border-bottom:1px solid var(--line)}.fact:last-child{border-bottom:0}
  .account-equity{font-size:25px}.definition-grid{grid-template-columns:1fr}.definition{border-right:0;border-bottom:1px solid var(--line)}.definition:last-child{border-bottom:0}
  .table-wrap{overflow:visible}
  table.mobile-table{min-width:0}
  table.mobile-table thead{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
  table.mobile-table,table.mobile-table tbody,table.mobile-table tr,table.mobile-table td{display:block;width:100%}
  table.mobile-table tr{padding:9px 0;border-bottom:1px solid var(--line)}table.mobile-table tr:last-child{border-bottom:0}
  table.mobile-table td{display:grid;grid-template-columns:92px minmax(0,1fr);gap:8px;padding:5px 14px;border:0;white-space:normal;min-width:0}
  table.mobile-table td::before{content:attr(data-label);font-size:11px;color:var(--muted);font-weight:700}
  table.mobile-table td[colspan]{display:block;text-align:center;padding:18px 14px}
  table.mobile-table td[colspan]::before{content:none}.issue{grid-template-columns:10px minmax(0,1fr)}.issue-count{grid-column:2}
}
</style>
</head>
<body>
<header class="topbar">
  <div class="topbar-inner">
    <div class="brand"><h1>模拟交易控制台</h1><p>账户、策略、AI 决策与运行状态</p></div>
    <div class="runtime-strip" aria-label="当前运行状态">
      <div class="runtime-item"><span id="service-dot" class="dot"></span><span><span class="runtime-label">服务</span><br><span id="service-value" class="runtime-value">读取中</span></span></div>
      <div class="runtime-item"><span id="market-dot" class="dot info"></span><span><span class="runtime-label">市场</span><br><span id="market-value" class="runtime-value">读取中</span></span></div>
      <div class="runtime-item"><span id="fresh-dot" class="dot"></span><span><span class="runtime-label">股票报价</span><br><span id="fresh-value" class="runtime-value">读取中</span></span></div>
    </div>
  </div>
</header>
<div class="paper-boundary" id="paper-boundary"><div class="paper-boundary-inner"><strong id="paper-boundary-label">正在验证模拟安全边界</strong><span id="paper-boundary-detail">尚未读取运行模式</span></div></div>
<nav class="tabbar" aria-label="控制台视图">
  <div class="tabs" role="tablist" aria-label="模拟交易控制台">
    <button class="tab" id="tab-overview" data-tab="overview" role="tab" aria-controls="panel-overview" aria-selected="true" tabindex="0">总览</button>
    <button class="tab" id="tab-portfolio" data-tab="portfolio" role="tab" aria-controls="panel-portfolio" aria-selected="false" tabindex="-1">持仓与订单</button>
    <button class="tab" id="tab-strategies" data-tab="strategies" role="tab" aria-controls="panel-strategies" aria-selected="false" tabindex="-1">策略表现</button>
    <button class="tab" id="tab-ai" data-tab="ai" role="tab" aria-controls="panel-ai" aria-selected="false" tabindex="-1">AI 决策</button>
    <button class="tab" id="tab-health" data-tab="health" role="tab" aria-controls="panel-health" aria-selected="false" tabindex="-1">系统健康</button>
  </div>
</nav>
<main>
  <section class="view" id="panel-overview" role="tabpanel" aria-labelledby="tab-overview"><div id="view-overview"><div class="empty-state">正在读取本地模拟状态...</div></div></section>
  <section class="view" id="panel-portfolio" role="tabpanel" aria-labelledby="tab-portfolio" hidden><div id="view-portfolio" class="empty-state"><h2>持仓与订单</h2><p>正在整理账户中的持仓和订单。</p></div></section>
  <section class="view" id="panel-strategies" role="tabpanel" aria-labelledby="tab-strategies" hidden><div id="view-strategies" class="empty-state"><h2>策略表现</h2><p>正在整理各条策略线的独立结果。</p></div></section>
  <section class="view" id="panel-ai" role="tabpanel" aria-labelledby="tab-ai" hidden><div id="view-ai" class="empty-state"><h2>AI 决策</h2><p>正在整理候选、证据和确定性风控结论。</p></div></section>
  <section class="view" id="panel-health" role="tabpanel" aria-labelledby="tab-health" hidden><div id="view-health" class="empty-state"><h2>系统健康</h2><p>正在整理当前故障和历史运行记录。</p></div></section>
</main>
<script>
const REFRESH_INTERVAL_MS=15000;
const TAB_IDS=["overview","portfolio","strategies","ai","health"];
const esc=value=>String(value??"—").replace(/[&<>'"]/g,char=>({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[char]));
const number=value=>Number(value||0);
const array=value=>Array.isArray(value)?value:[];
const money=value=>value===undefined||value===null?"—":"$"+Math.abs(number(value)).toFixed(2);
const signedMoney=value=>(number(value)>0?"+":number(value)<0?"-":"")+money(value);
const pct=value=>value===undefined||value===null?"—":number(value).toFixed(2)+"%";
const tone=value=>number(value)>0?"good-text":number(value)<0?"bad-text":"";
function ageLabel(seconds){
  if(seconds===undefined||seconds===null)return "无时间戳";
  const value=Math.max(0,number(seconds));
  if(value<60)return `${value.toFixed(0)} 秒前`;
  if(value<3600)return `${(value/60).toFixed(value<600?1:0)} 分钟前`;
  if(value<86400)return `${(value/3600).toFixed(1)} 小时前`;
  return `${(value/86400).toFixed(1)} 天前`;
}
const serviceLabels={ok:"运行中",stale:"心跳过期",stopped:"已停止",degraded:"部分降级",unknown:"未知"};
const marketLabels={pre_market:"盘前",regular:"正常交易",post_market:"盘后",after_hours:"盘后",closed:"休市"};
const completedOrderStatuses=new Set(["filled","cancelled","expired","rejected"]);
function isUnfinishedOrder(status){return !completedOrderStatuses.has(status)}
function statusBadge(label,kind=""){
  return `<span class="status-badge ${kind}">${esc(label)}</span>`;
}
function serviceStatusKind(status){
  if(status==="unknown")return "";
  if(status==="ok")return "good";
  if(status==="degraded")return "warn";
  return ["stale","stopped"].includes(status)?"bad":"";
}
function setDot(id,kind){document.getElementById(id).className="dot "+kind}
function paperOnlyMode(mode){return mode.paper===true&&mode.live_readonly===false&&mode.live_trading===false}
function metricTotalPnl(metrics){
  const value=metrics||{};
  if(value.ending_equity!=null&&value.initial_cash!=null)return number(value.ending_equity)-number(value.initial_cash);
  return value.realized_pnl??null;
}
function metricEntryCount(metrics){
  const value=metrics||{};
  if(value.closed_trade_count==null&&value.open_position_count==null)return null;
  return number(value.closed_trade_count)+number(value.open_position_count);
}
function evidenceConclusion(evidence){
  const value=evidence||{};
  if(value.promotion_eligible===true)return "已通过当前盈利与风控门槛";
  if(value.sufficient!==true)return "样本数量或质量仍不足";
  return "样本数量已达标，但结果未通过盈利门槛";
}
function renderBoundary(state){
  const mode=state.mode||{},safe=paperOnlyMode(mode),boundary=document.getElementById("paper-boundary");
  boundary.classList.toggle("bad",!safe);
  document.getElementById("paper-boundary-label").textContent=safe?"Paper only · 仅使用假钱模拟":"安全模式异常 · 不能视为纯模拟";
  document.getElementById("paper-boundary-detail").textContent=safe?"Robinhood 行情只读 · 不会调用真实下单工具":`paper=${mode.paper===true} · live_readonly=${mode.live_readonly===true} · live_trading=${mode.live_trading===true}`;
}
function currentAlerts(state){
  const alerts=[];
  const heartbeat=state.heartbeat||{},status=heartbeat.effective_status||"unknown";
  if(status!=="ok")alerts.push({kind:serviceStatusKind(status),text:`主服务${serviceLabels[status]||status}`});
  if(!paperOnlyMode(state.mode||{}))alerts.push({kind:"bad",text:"Paper-only 三元安全边界不符合预期"});
  const jobs=(((heartbeat.payload||{}).latest_jobs)||{});
  Object.entries(jobs).forEach(([name,job])=>{
    const jobStatus=(job||{}).status;
    if(["failed","timed_out"].includes(jobStatus))alerts.push({kind:"bad",text:`${name} 最近一次作业${jobStatus==="failed"?"失败":"超时"}`});
  });
  return alerts;
}
function renderHeader(state){
  const b=state.beginner_summary||{},service=b.service||{},heartbeat=state.heartbeat||{};
  const status=service.status||heartbeat.effective_status||"unknown";
  document.getElementById("service-value").textContent=serviceLabels[status]||status;
  setDot("service-dot",serviceStatusKind(status));
  const session=service.market_session||"unknown";
  document.getElementById("market-value").textContent=heartbeat.stale?"状态已过期":marketLabels[session]||"未识别";
  setDot("market-dot",heartbeat.stale?"warn":session==="regular"?"good":"info");
  const equityQuote=(state.market_data||{}).equity||{},age=equityQuote.age_seconds;
  document.getElementById("fresh-value").textContent=ageLabel(age);
  setDot("fresh-dot",age==null?"":equityQuote.stale?"bad":"good");
}
function systemActivity(state){
  const b=state.beginner_summary||{},service=b.service||{},account=b.account||{};
  const status=service.status||"unknown",positions=number(account.open_equity_positions)+number(account.open_option_positions);
  if(status!=="ok")return {title:"主服务当前没有正常推进",copy:"系统保持 fail-closed，不会使用不完整数据创建模拟订单。",kind:serviceStatusKind(status)};
  if(positions>0)return {title:`正在监控 ${positions} 个持仓`,copy:"退出模块会继续检查价格、盈亏、持有期限和收盘前平仓规则。",kind:"info"};
  if(service.market_session==="regular")return {title:"正在扫描机会，目前没有持仓",copy:"系统会先筛选候选，再经过新闻、模型判断和确定性风控；没有通过时不会强行交易。",kind:"good"};
  return {title:"市场当前不在正常交易时段",copy:"系统会更新研究和状态，但只在允许的正常交易时段模拟开仓。",kind:"info"};
}
function accountCard({title,subtitle,equity,initial,pnl,cash,positions,orders,badge,badgeKind}){
  return `<article class="account-card">
    <div class="account-head"><div><h3>${esc(title)}</h3><p>${esc(subtitle)}</p></div>${statusBadge(badge,badgeKind)}</div>
    <div class="account-body">
      <div class="account-primary"><div class="eyebrow">当前净值</div><div class="account-equity">${money(equity)}</div><div class="account-pnl ${tone(pnl)}">累计 ${signedMoney(pnl)}</div><div class="small muted">初始 ${money(initial)}</div></div>
      <div class="account-facts">
        <div class="fact"><div class="fact-label">可用现金</div><div class="fact-value">${money(cash)}</div></div>
        <div class="fact"><div class="fact-label">当前持仓</div><div class="fact-value">${positions} 个</div></div>
        <div class="fact"><div class="fact-label">未完成订单</div><div class="fact-value">${orders} 笔</div></div>
        <div class="fact"><div class="fact-label">累计收益率</div><div class="fact-value ${tone(pnl)}">${initial?pct(number(pnl)/number(initial)*100):"—"}</div></div>
      </div>
    </div>
  </article>`;
}
function strategySummaryRows(state){
  const b=state.beginner_summary||{},lines=b.strategy_lines||{},metrics=state.metrics||{},metricLines=metrics.lines||{};
  const equity=lines.equity||{},options=lines.options||{},ai=lines.ai||{},aiMetrics=((state.ai_gated||{}).metrics)||{},allocator=state.ai_instrument_allocator||{},allocatorMetrics=allocator.metrics||{};
  const rows=[
    {name:"股票加权",mode:(state.strategy_modes||{}).weighted_relative_strength_v2==="shadow_only"?"只观察":"模拟交易",kind:"info",pnl:(metricLines.equity||{}).net_pnl,activity:`监控 ${equity.watchlist_count||0} 个标的`},
    {name:"方向期权",mode:(state.strategy_modes||{}).long_directional_options_v2_weighted_new_entries?"模拟交易":"只管理旧仓",kind:"warn",pnl:(metricLines.options||{}).net_pnl,activity:`${options.direction_evaluations||0} 次方向评估`},
    {name:"旧 AI Gated",mode:(state.strategy_modes||{}).ai_gated_technical_v1_new_entries?"模拟交易":"影子研究 / 管理旧仓",kind:"warn",pnl:metricTotalPnl(aiMetrics),activity:`${ai.completed||0} 次完成`},
    {name:"AI 工具分配器",mode:String((state.strategy_modes||{}).ai_instrument_allocator_v1||"").includes("paper")?"模拟交易":"未启用",kind:"good",pnl:metricTotalPnl(allocatorMetrics),activity:`${array(allocator.positions).length+array(allocator.option_positions).length} 个持仓`},
  ];
  return rows.map(row=>`<tr><td data-label="策略"><strong>${esc(row.name)}</strong></td><td data-label="状态">${statusBadge(row.mode,row.kind)}</td><td data-label="累计 PnL" class="num ${tone(row.pnl)}">${signedMoney(row.pnl)}</td><td data-label="最近活动" class="muted">${esc(row.activity)}</td></tr>`).join("");
}
function validationStatusLabel(status){
  return ({passed:"已通过",failed:"未通过",not_run:"未运行",performance_ready:"可做收益评估",diagnostic_only:"仅诊断",not_generated:"未生成",forward_evidence_sufficient:"样本已达标",insufficient_forward_evidence:"样本不足"})[status]||status||"未知";
}
function validationStatusKind(status){
  if(["passed","performance_ready","forward_evidence_sufficient"].includes(status))return "good";
  if(status==="failed")return "bad";
  return "warn";
}
function renderValidationEvidence(state){
  const validation=state.allocator_validation||{},lines=array(validation.evidence_lines);
  const cards=lines.map(line=>`<div class="definition"><div style="display:flex;justify-content:space-between;gap:10px;align-items:center"><strong>${esc(line.title)}</strong>${statusBadge(validationStatusLabel(line.status),validationStatusKind(line.status))}</div><span>${esc(line.detail)}</span></div>`).join("");
  const optionCopy=validation.option_claim==="executable_option_pnl"?"历史期权链字段完整，可进入独立的可执行期权回测。":"历史期权链尚不完整：只能看 synthetic option sensitivity（不可执行估算），不能把它当成期权历史 PnL。";
  return `<section class="section-block"><div class="section-head"><div><h3>三种证据不要混淆</h3><p>功能能跑通、历史数据表现、真实向前模拟是三件不同的事。</p></div><div class="asof">${validation.generated_at?`报告 ${localDateTime(validation.generated_at)}`:"尚未生成验证报告"}</div></div><div class="definition-grid">${cards}</div><div class="clear-state" style="margin-top:12px">${esc(optionCopy)}</div></section>`;
}
function renderOverview(state){
  const b=state.beginner_summary||{},day=b.day||{},legacy=b.account||{},allocator=state.ai_instrument_allocator||{},allocatorAccount=allocator.account||{},allocatorMetrics=allocator.metrics||{};
  const activity=systemActivity(state),alerts=currentAlerts(state);
  const legacyOrders=array(state.orders).filter(order=>isUnfinishedOrder(order.status)).length+array(state.option_orders).filter(order=>isUnfinishedOrder(order.status)).length;
  const allocatorOrders=array(allocator.orders).filter(order=>isUnfinishedOrder(order.status)).length+array(allocator.option_orders).filter(order=>isUnfinishedOrder(order.status)).length;
  const legacyCard=accountCard({title:"旧 $2,000 模拟账本",subtitle:"历史交易与旧策略持仓",equity:legacy.ending_equity,initial:legacy.initial_cash,pnl:legacy.cumulative_pnl,cash:(state.account||{}).cash,positions:number(legacy.open_equity_positions)+number(legacy.open_option_positions),orders:legacyOrders,badge:"独立账本",badgeKind:"info"});
  const allocatorInitial=allocatorAccount.initial_cash||allocatorMetrics.initial_cash||10000;
  const allocatorEquity=allocatorMetrics.ending_equity??allocatorAccount.cash;
  const allocatorPnl=metricTotalPnl(allocatorMetrics)??(allocatorEquity==null?null:number(allocatorEquity)-number(allocatorInitial));
  const allocatorCard=accountCard({title:"$10,000 AI 分配账户",subtitle:"ai_instrument_allocator_v1",equity:allocatorEquity,initial:allocatorInitial,pnl:allocatorPnl,cash:allocatorAccount.cash,positions:array(allocator.positions).length+array(allocator.option_positions).length,orders:allocatorOrders,badge:"独立账户",badgeKind:"good"});
  const alertHtml=alerts.length?alerts.map(item=>`<div class="alert-row ${item.kind}"><span class="alert-mark"></span><span>${esc(item.text)}</span></div>`).join(""):'<div class="clear-state">当前没有检测到阻塞运行的故障</div>';
  document.getElementById("view-overview").innerHTML=`
    <div class="page-heading"><div><h2>总览</h2><p>先看系统状态和两套模拟账户，再决定是否需要查看细节。</p></div><div class="asof">最近交易日 ${esc(b.session_date||"—")} · 页面刷新 ${new Date().toLocaleTimeString("zh-CN",{hour12:false})}</div></div>
    <div class="activity">
      <section class="activity-main"><div class="eyebrow">系统现在在做什么</div><div class="activity-title">${esc(activity.title)}</div><p class="activity-copy">${esc(activity.copy)}</p><div style="margin-top:12px">${statusBadge(activity.kind==="bad"?"需要处理":activity.kind==="good"?"正常运行":"当前状态",activity.kind)}</div></section>
      <aside class="current-alerts"><div class="alert-head"><h3>当前阻塞事项</h3>${statusBadge(alerts.length?alerts.length+" 项":"无",alerts.length?"bad":"good")}</div><div class="alert-list">${alertHtml}</div></aside>
    </div>
    <div class="account-grid">${legacyCard}${allocatorCard}</div>
    <section class="section-block"><div class="section-head"><div><h3>最近交易日结果</h3><p>只把已完成买入和卖出的闭环计入当日结果。</p></div><strong class="num ${tone(day.realized_pnl)}">${signedMoney(day.realized_pnl)}</strong></div>
      <div class="table-wrap"><table class="mobile-table"><thead><tr><th>已平仓</th><th>盈利</th><th>亏损</th><th>当前持仓</th><th>评估结论</th></tr></thead><tbody><tr><td data-label="已平仓" class="num">${day.closed_trades||0} 笔</td><td data-label="盈利" class="num good-text">${day.wins||0} 笔</td><td data-label="亏损" class="num bad-text">${day.losses||0} 笔</td><td data-label="当前持仓" class="num">${number(legacy.open_equity_positions)+number(legacy.open_option_positions)} 个</td><td data-label="评估结论">${esc(evidenceConclusion(b.evidence))}</td></tr></tbody></table></div>
    </section>
    ${renderValidationEvidence(state)}
    <section class="section-block"><div class="section-head"><div><h3>策略状态速览</h3><p>每条策略的账户和交易权限相互区分。</p></div></div><div class="table-wrap"><table class="mobile-table"><thead><tr><th>策略</th><th>当前模式</th><th>累计 PnL</th><th>最近活动</th></tr></thead><tbody>${strategySummaryRows(state)}</tbody></table></div></section>`;
}
function localDateTime(value){
  if(!value)return "—";
  const parsed=new Date(value);
  return Number.isNaN(parsed.getTime())?"—":parsed.toLocaleString("zh-CN",{month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hour12:false});
}
function humanReason(value){
  const reason=String(value||"");let match;
  if(!reason)return "没有记录原因";
  if(reason.includes("future option quote would create lookahead"))return "期权报价时间早于决策时间，系统拒绝使用";
  if(reason.includes("future quote would create lookahead"))return "报价时间晚于决策截止点，系统拒绝使用";
  if(reason.includes("spread too wide"))return "买卖价差过大，预估成交成本过高";
  if(reason.includes("insufficient option volume"))return "期权成交量不足";
  if(reason.includes("insufficient option open interest"))return "期权未平仓量不足";
  if(reason.includes("outside regular market session"))return "当前不在美股正常交易时段";
  if(reason.includes("stale"))return "行情或证据已经过期";
  if(reason.includes("mandatory pre-close flatten"))return "按规则在收盘前平仓";
  if(reason.includes("existing position is managed by the exit pipeline"))return "已有持仓，继续交由退出模块管理";
  if(reason.includes("binary earnings event inside"))return "临近财报，事件波动风险过高";
  if((match=reason.match(/weighted technical score ([0-9.]+) below ([0-9.]+)/)))return `综合分 ${match[1]}，低于入场线 ${match[2]}`;
  if((match=reason.match(/best weighted option score below ([0-9.]+)/)))return `期权方向分数低于入场线 ${match[1]}`;
  return reason;
}
function firstReason(value,fallback="没有记录原因"){
  const values=array(value).filter(Boolean);
  return values.length?values.map(humanReason).join("；"):fallback;
}
function expandableEvidence(value){
  const text=String(value||"—").trim();
  if(text.length<=180)return esc(text);
  return `<details class="evidence-details"><summary>${esc(text.slice(0,180).trim())}…</summary><div>${esc(text)}</div></details>`;
}
function optionDisplay(contract){
  const type=contract.option_type==="put"?"看跌 Put":contract.option_type==="call"?"看涨 Call":"期权";
  return `${contract.underlying||"—"} ${type} · ${contract.expiration_date||"—"} · $${contract.strike_price??"—"}`;
}
function orderStatusLabel(status){
  return ({created:"已创建",submitted_to_paper_broker:"已提交模拟券商",open:"等待成交",partially_filled:"部分成交",filled:"已成交",cancelled:"已取消",expired:"已过期",rejected:"已拒绝"})[status]||status||"未知";
}
function orderStatusKind(status){
  if(status==="filled")return "good";
  if(["rejected","expired","cancelled"].includes(status))return status==="cancelled"?"":"bad";
  return isUnfinishedOrder(status)?"warn":"";
}
function mandateExitPlan(mandate){
  const plan=mandate||{},planned=plan.planned_exit_at?`最晚计划退出 ${localDateTime(plan.planned_exit_at)}`:"退出模块持续监控";
  return plan.invalidation_condition?`${planned}；研究失效条件仅记录，当前不自动判定：${plan.invalidation_condition}`:planned;
}
function collectPositions(state){
  const rows=[];
  const allocator=state.ai_instrument_allocator||{},ai=state.ai_gated||{};
  const addEquity=(items,account,strategy,mandates=[])=>array(items).forEach(position=>{
    const mandate=array(mandates).find(item=>item.ticker===position.symbol&&item.status==="open")||{};
    const exitPlan=Object.keys(mandate).length?mandateExitPlan(mandate):humanReason((position.exit_evaluation||{}).reason||"退出模块持续监控");
    rows.push({account,strategy,instrument:"股票",symbol:position.symbol||"—",direction:"看涨 Long",quantity:position.quantity,average:position.average_price,cost:number(position.quantity)*number(position.average_price),pnl:position.unrealized_pnl,opened:position.opened_at,status:"持有中",exit:exitPlan});
  });
  const addOptions=(items,account,strategy,mandates=[])=>array(items).forEach(position=>{
    const contract=position.contract||{},mandate=array(mandates).find(item=>item.ticker===contract.underlying&&item.status==="open")||{};
    rows.push({account,strategy,instrument:contract.option_type==="put"?"看跌 Put":"看涨 Call",symbol:optionDisplay(contract),direction:contract.option_type==="put"?"看跌":"看涨",quantity:position.quantity,average:position.average_price,cost:number(position.quantity)*number(position.average_price)*number(contract.multiplier||100),pnl:position.unrealized_pnl,opened:position.opened_at,status:"持有中",exit:Object.keys(mandate).length?mandateExitPlan(mandate):"退出模块持续监控"});
  });
  addEquity(state.positions,"旧 $2,000 账本","股票/旧策略");
  addOptions(state.option_positions,"旧 $2,000 账本","方向期权");
  addEquity(ai.positions,"旧 AI sleeve","旧 AI Gated");
  addOptions(ai.option_positions,"旧 AI sleeve","旧 AI Gated");
  addEquity(allocator.positions,"$10,000 allocator","AI 工具分配器",allocator.mandates);
  addOptions(allocator.option_positions,"$10,000 allocator","AI 工具分配器",allocator.mandates);
  return rows;
}
function collectOrders(state){
  const rows=[];
  const addEquity=(items,account,strategy)=>array(items).forEach(order=>rows.push({account,strategy,instrument:"股票",symbol:order.symbol||"—",action:order.side==="sell"?"卖出":"买入",quantity:order.quantity,price:order.average_fill_price??order.limit_price,status:order.status,time:order.submitted_at||order.created_at,reason:order.reject_reason||order.thesis||"—"}));
  const addOptions=(items,account,strategy)=>array(items).forEach(order=>rows.push({account,strategy,instrument:(order.contract||{}).option_type==="put"?"看跌 Put":"看涨 Call",symbol:optionDisplay(order.contract||{}),action:order.intent==="sell_to_close"?"卖出平仓":"买入开仓",quantity:order.quantity,price:order.average_fill_price??order.limit_price,status:order.status,time:order.submitted_at||order.created_at,reason:order.reject_reason||order.thesis||"—"}));
  addEquity(state.orders,"旧 $2,000 账本","股票/旧策略");
  addOptions(state.option_orders,"旧 $2,000 账本","方向期权");
  const ai=state.ai_gated||{};addEquity(ai.orders,"旧 AI sleeve","旧 AI Gated");addOptions(ai.option_orders,"旧 AI sleeve","旧 AI Gated");
  const allocator=state.ai_instrument_allocator||{};addEquity(allocator.orders,"$10,000 allocator","AI 工具分配器");addOptions(allocator.option_orders,"$10,000 allocator","AI 工具分配器");
  return rows.sort((left,right)=>String(right.time||"").localeCompare(String(left.time||"")));
}
function positionTable(rows){
  const body=rows.map(row=>`<tr data-record-type="position"><td data-label="账户"><strong>${esc(row.account)}</strong><div class="small muted">${esc(row.strategy)}</div></td><td data-label="标的"><strong>${esc(row.symbol)}</strong><div class="small muted">${esc(row.instrument)} · ${esc(row.direction)}</div></td><td data-label="数量" class="num">${esc(row.quantity)}</td><td data-label="平均成本" class="num">${money(row.average)}<div class="small muted">占用 ${money(row.cost)}</div></td><td data-label="未实现 PnL" class="num ${tone(row.pnl)}">${row.pnl==null?"暂无估值":signedMoney(row.pnl)}</td><td data-label="状态">${statusBadge(row.status,"good")}</td><td data-label="退出计划" class="reason">${esc(row.exit)}<div class="small muted">开仓 ${localDateTime(row.opened)}</div></td></tr>`).join("");
  return `<div class="table-wrap"><table class="mobile-table"><thead><tr><th>账户</th><th>标的与方向</th><th>数量</th><th>平均成本</th><th>未实现 PnL</th><th>状态</th><th>退出计划</th></tr></thead><tbody>${body||'<tr><td colspan="7" class="muted">当前没有持仓。</td></tr>'}</tbody></table></div>`;
}
function orderTable(rows,emptyText,limit=20){
  const displayed=limit==null?rows:rows.slice(0,limit);
  const body=displayed.map(row=>`<tr data-record-type="order"><td data-label="账户"><strong>${esc(row.account)}</strong><div class="small muted">${esc(row.strategy)}</div></td><td data-label="标的"><strong>${esc(row.symbol)}</strong><div class="small muted">${esc(row.instrument)}</div></td><td data-label="动作">${esc(row.action)}</td><td data-label="数量" class="num">${esc(row.quantity)}</td><td data-label="价格" class="num">${money(row.price)}</td><td data-label="状态">${statusBadge(orderStatusLabel(row.status),orderStatusKind(row.status))}</td><td data-label="时间与原因" class="reason">${expandableEvidence(humanReason(row.reason))}<div class="small muted">${localDateTime(row.time)}</div></td></tr>`).join("");
  return `<div class="table-wrap"><table class="mobile-table"><thead><tr><th>账户</th><th>标的</th><th>动作</th><th>数量</th><th>价格</th><th>状态</th><th>时间与原因</th></tr></thead><tbody>${body||`<tr><td colspan="7" class="muted">${esc(emptyText)}</td></tr>`}</tbody></table></div>`;
}
function renderPortfolio(state){
  const positions=collectPositions(state),orders=collectOrders(state),openOrders=orders.filter(order=>isUnfinishedOrder(order.status)),history=orders.filter(order=>!isUnfinishedOrder(order.status));
  const historySummary=state.order_history_summary||{},historyTotal=historySummary.completed_total??history.length,historyShown=Math.min(history.length,20);
  document.getElementById("view-portfolio").className="";
  document.getElementById("view-portfolio").innerHTML=`<div class="page-heading"><div><h2>持仓与订单</h2><p>订单只有成交后才会成为持仓；两套账户不会合并。</p></div><div class="asof">${positions.length} 个持仓 · ${openOrders.length} 笔未完成订单</div></div>
    <section class="section-block"><div class="section-head"><div><h3>当前持仓</h3><p>股票和期权统一展示，账户归属保持独立。</p></div>${statusBadge(positions.length+" 个",positions.length?"info":"")}</div>${positionTable(positions)}</section>
    <section class="section-block"><div class="section-head"><div><h3>未完成订单</h3><p>已创建、已提交、等待成交和部分成交均不计为完整持仓。</p></div>${statusBadge(openOrders.length+" 笔",openOrders.length?"warn":"good")}</div>${orderTable(openOrders,"当前没有等待成交的订单。",null)}</section>
    <section class="section-block"><details class="history-details"><summary>最近订单记录 · ${historyShown} / ${historyTotal} 笔<span>展开查看最近 ${historyShown} 笔已成交、拒绝、取消或过期订单。</span></summary>${orderTable(history,"尚无已结束订单。")}</details></section>`;
}
function strategyDetailRows(state){
  const b=state.beginner_summary||{},lines=b.strategy_lines||{},metrics=state.metrics||{},metricLines=metrics.lines||{},ai=state.ai_gated||{},allocator=state.ai_instrument_allocator||{},news=state.news_drift||{};
  const aiMetrics=ai.metrics||{},allocatorMetrics=allocator.metrics||{},allocatorFunnel=((state.trade_funnel||{}).allocator)||{};
  const latestCandidate=array(state.candidates).slice().sort((a,b)=>number(b.score)-number(a.score))[0]||{};
  const latestOption=array(state.option_decisions).slice().sort((a,b)=>Math.max(number(b.call_score),number(b.put_score))-Math.max(number(a.call_score),number(a.put_score)))[0]||{};
  const latestAi=array(ai.decisions).slice(-1)[0]||{},latestAllocation=allocator.latest_allocation||{},newsMetrics=news.metrics||{},newsNext=(((newsMetrics.horizons||{}).next_close||{}).portfolio_day)||{};
  return [
    {name:"weighted_relative_strength_v2",label:"股票加权",mode:(state.strategy_modes||{}).weighted_relative_strength_v2==="shadow_only"?"只观察":"模拟交易",kind:"info",account:"旧 $2,000",decisions:(lines.equity||{}).watchlist_count,entries:(lines.equity||{}).entries,closed:(metricLines.equity||{}).closed_trade_count,pnl:(metricLines.equity||{}).net_pnl,win:(metricLines.equity||{}).win_rate,reason:firstReason(latestCandidate.reasons,latestCandidate.action==="buy"?"最近候选达到技术门槛":"暂无候选")},
    {name:"long_directional_options_v2_weighted",label:"方向期权",mode:(state.strategy_modes||{}).long_directional_options_v2_weighted_new_entries?"模拟交易":"只管理旧仓",kind:"warn",account:"旧 $2,000",decisions:(lines.options||{}).direction_evaluations,entries:(lines.options||{}).orders,closed:(metricLines.options||{}).closed_trade_count,pnl:(metricLines.options||{}).net_pnl,win:(metricLines.options||{}).win_rate,reason:firstReason(latestOption.reasons,"最近没有通过合约筛选")},
    {name:"ai_gated_technical_v1",label:"旧 AI Gated",mode:(state.strategy_modes||{}).ai_gated_technical_v1_new_entries?"模拟交易":"影子研究 / 管理旧仓",kind:"warn",account:"旧 AI sleeve",decisions:(lines.ai||{}).cycles,entries:metricEntryCount(aiMetrics),closed:aiMetrics.closed_trade_count,pnl:metricTotalPnl(aiMetrics),win:aiMetrics.win_rate,reason:humanReason((latestAi.execution||{}).reason||(latestAi.decision||{}).no_trade_reason||(latestAi.decision||{}).thesis||"暂无最近决策")},
    {name:"ai_instrument_allocator_v1",label:"AI 工具分配器",mode:String((state.strategy_modes||{}).ai_instrument_allocator_v1||"").includes("paper")?"模拟交易":"未启用",kind:"good",account:"$10,000 allocator",decisions:allocatorFunnel.model_decisions??array(allocator.decisions).length,entries:metricEntryCount(allocatorMetrics),closed:allocatorMetrics.closed_trade_count,pnl:metricTotalPnl(allocatorMetrics),win:allocatorMetrics.win_rate,reason:humanReason(latestAllocation.reason||((latestAllocation.selected_instrument||{}).ticker?"已选择工具，等待执行或持仓管理":"最近没有选择可执行工具"))},
    {name:"llm_news_drift_v1",label:"新闻漂移实验",mode:"只观察",kind:"info",account:"不进入账户",decisions:newsMetrics.proposal_count,entries:0,closed:"—",pnl:null,win:newsNext.hit_rate,reason:`${newsMetrics.valid_return_label_count||0} 个有效结果标签 · ${newsMetrics.profitability||"insufficient_forward_evidence"}`},
  ];
}
function renderOpportunityFunnel(state){
  const funnel=state.trade_funnel||{},a=funnel.allocator||{},root=funnel.root_cause||{},baselines=funnel.baselines||{},equity=baselines.equity||{},options=baselines.options||{},replay=funnel.replay_comparison||{},oldPolicy=replay.old_policy||{},newPolicy=replay.new_policy||{},observed=replay.observed_audit_funnel||{},comparison=replay.comparison||{},pointInTime=replay.point_in_time||{};
  const steps=[
    {label:"候选被发现",value:a.candidate_reviews,detail:"原始候选；尚未调用深度模型"},
    {label:"进入 AI 排名",value:a.ranking_inputs,detail:"通过 cooldown 后的 ranking input"},
    {label:"完成深度研究",value:a.deep_research,detail:"保存 point-in-time 证据后研究"},
    {label:"结构化结论",value:a.model_decisions,detail:`硬否决 ${a.hard_veto||0} / 不交易 ${a.model_no_trade||0} / 观察 ${a.watch||0} / 提案 ${a.trade_proposals||0}`},
    {label:"工具分配 / 选中",value:`${a.allocation_attempts||0} / ${a.selected_instruments||0}`,detail:"股票与期权重新定价及确定性门槛"},
    {label:"订单 / 成交",value:`${a.paper_orders||0} / ${a.paper_fills||0}`,detail:"仅 $10,000 paper sleeve"},
  ];
  const stepHtml=steps.map(step=>`<div class="pipeline-step"><strong>${esc(step.label)} · ${esc(step.value??0)}</strong><span>${esc(step.detail)}</span></div>`).join("");
  const blockers=array(funnel.blockers).slice(0,6).map(item=>`<div class="issue ${item.kind==="bad"?"error":""}"><span class="issue-mark"></span><div><div class="issue-title">${esc(item.label)}</div><div class="issue-copy">${esc(item.explanation)}</div></div><div class="issue-count">${esc(item.count)} 次</div></div>`).join("");
  const proposalExplanation=number(comparison.proposal_delta)===0?"严格回放不会补造缺失的模型判断，也不会把旧版模糊否决猜成新提案，所以可重放子集的 proposal 数不变。":"已有结构化且时间有效的历史结论在新规则下增加了 proposal。";
  const estimatedAvoidable=number(comparison.estimated_avoidable_rank_only_cooldowns);
  const replayHtml=Object.keys(oldPolicy).length?`<div class="definition-grid"><div class="definition"><strong>旧规则 → 新规则（严格子集）</strong><span>可重放候选 ${esc(oldPolicy.candidates||0)}；进入排名 ${esc(oldPolicy.ranking_input||0)} → ${esc(newPolicy.ranking_input||0)}；Watch ${esc(oldPolicy.watch||0)} → ${esc(newPolicy.watch||0)}；Proposal ${esc(oldPolicy.proposals||0)} → ${esc(newPolicy.proposals||0)}。</span></div><div class="definition"><strong>历史 cooldown 影响估计</strong><span>观察日志显示 ranking input ${esc(observed.ranking_input||0)}；估计有 ${esc(estimatedAvoidable)} 次 rank-only cooldown 可避免。旧日志缺少逐候选关联，不能把该估计写成精确新漏斗。</span></div><div class="definition"><strong>回放证据边界</strong><span>${esc(proposalExplanation)} 排除时间不合格 snapshot ${esc(pointInTime.excluded_snapshot_count||0)} 个；历史订单 ${esc(replay.historical_orders_created||0)}；真实下单工具 ${replay.live_order_tools_called?"曾调用（异常）":"未调用"}。</span></div></div>`:"";
  return `<section class="section-block"><div class="section-head"><div><h3>过去 48 小时观测审计漏斗</h3><p>冻结时间 ${localDateTime(funnel.asof)}；这是实际日志计数，不是严格 point-in-time 回放，候选、观察和提案都不代表订单。</p></div>${statusBadge(root.title||"正在统计",(a.paper_orders||0)>0?"good":"warn")}</div>
    <div class="pipeline opportunity-pipeline">${stepHtml}</div>
    <div class="stat-strip"><div class="stat"><div class="stat-label">硬否决 Hard veto</div><div class="stat-value">${esc(a.hard_veto||0)}</div></div><div class="stat"><div class="stat-label">软顾虑 Soft concern</div><div class="stat-value">${esc(a.soft_concern||0)}</div></div><div class="stat"><div class="stat-label">观察 Watch</div><div class="stat-value">${esc(a.watch||0)}</div></div><div class="stat"><div class="stat-label">交易提案 Proposal</div><div class="stat-value">${esc(a.trade_proposals||0)}</div></div></div>
    <div class="issue-list"><div class="issue"><span class="issue-mark"></span><div><div class="issue-title">自动定位：${esc(root.title||"暂无结论")}</div><div class="issue-copy">${esc(root.detail||"等待更多运行记录。")} 拒绝次数不是互斥人数：同一个候选可同时产生多份期权合约诊断。</div></div></div>${blockers}</div>
    ${replayHtml}
    <div class="definition-grid"><div class="definition"><strong>为什么股票信号没有下单</strong><span>股票加权过去 48 小时产生 ${esc(equity.signals||0)} 次候选信号，但当前是 shadow_only，只观察不新增仓。</span></div><div class="definition"><strong>为什么期权信号没有下单</strong><span>方向期权产生 ${esc(options.signals||0)} 次 buy_to_open 判断，但旧策略 entry_frozen，只管理旧仓。</span></div><div class="definition"><strong>当前 paper 方向门槛</strong><span>单侧未校准概率质量至少 ${(number(a.minimum_direction_mass)*100).toFixed(0)}%，且领先第二方向 ${(number(a.minimum_direction_margin)*100).toFixed(0)} 个百分点；仍须通过全部确定性风控。</span></div></div>
  </section>`;
}
function renderStrategies(state){
  const rows=strategyDetailRows(state).map(row=>`<tr><td data-label="策略"><strong>${esc(row.label)}</strong><div class="small muted">${esc(row.name)}</div></td><td data-label="状态">${statusBadge(row.mode,row.kind)}</td><td data-label="账户">${esc(row.account)}</td><td data-label="决策" class="num">${row.decisions??"—"}</td><td data-label="入场" class="num">${row.entries??"—"}</td><td data-label="平仓" class="num">${row.closed??"—"}</td><td data-label="累计 PnL" class="num ${tone(row.pnl)}">${signedMoney(row.pnl)}</td><td data-label="胜率" class="num">${row.win==null?"—":pct(number(row.win)*100)}</td><td data-label="最近结论" class="reason">${esc(row.reason)}</td></tr>`).join("");
  document.getElementById("view-strategies").className="";
  document.getElementById("view-strategies").innerHTML=`<div class="page-heading"><div><h2>策略表现</h2><p>同一行比较权限、账户、交易数量和结果；影子实验不会混入账户 PnL。</p></div></div>
    ${renderOpportunityFunnel(state)}
    <section class="section-block"><div class="table-wrap"><table class="mobile-table"><thead><tr><th>策略</th><th>当前模式</th><th>账户</th><th>决策</th><th>入场</th><th>平仓</th><th>累计 PnL</th><th>胜率</th><th>最近结论</th></tr></thead><tbody>${rows}</tbody></table></div>
      <div class="definition-grid"><div class="definition"><strong>胜率</strong><span>盈利交易数除以已平仓交易数，不能单独代表是否赚钱。</span></div><div class="definition"><strong>累计 PnL</strong><span>当前净值减去初始资金，包含已实现和当前未实现盈亏。</span></div><div class="definition"><strong>影子研究 / 管理旧仓</strong><span>继续记录不执行的 AI 决策，同时只对原有持仓执行退出管理。</span></div></div></section>`;
}
function renderAiDecisions(state){
  const allocator=state.ai_instrument_allocator||{},allocation=allocator.latest_allocation||{},selected=allocation.selected_instrument||{},decisions=array(allocator.decisions).slice().reverse().slice(0,12);
  const decisionRows=decisions.map(item=>{const signal=item.signal||{},challenge=item.challenge||{},hard=challenge.hard_veto===true||challenge.veto_recommended===true,soft=array(challenge.soft_concerns).length,actionLabel=signal.action==="propose_trade"?"交易提案":signal.action==="watch"?"观察":signal.action==="no_trade"?"不交易":signal.action||"不交易",challengeLabel=hard?statusBadge("硬否决","bad"):soft?statusBadge(`软顾虑 ${soft} 条`,"warn"):statusBadge("未否决","good");return `<tr><td data-label="时间" class="num">${localDateTime(item.asof)}</td><td data-label="股票"><strong>${esc(item.ticker)}</strong></td><td data-label="研究阶段">${esc(({overnight:"晚间研究",premarket_update:"盘前更新",preopen_revalidation:"开盘前复核",open_execution:"开盘执行",intraday:"盘中研究"})[item.stage]||item.stage)}</td><td data-label="方向">${statusBadge(actionLabel,signal.action==="propose_trade"?"good":signal.action==="watch"?"warn":"info")}</td><td data-label="期限">${esc(signal.horizon||"—")}</td><td data-label="Challenge">${challengeLabel}</td><td data-label="结构化结论" class="reason">${expandableEvidence(signal.thesis||signal.watch_reason||signal.no_trade_reason||"—")}</td></tr>`}).join("");
  const catalystRows=array(state.catalyst_decisions).slice().reverse().slice(0,6).map(item=>{const bull=item.bull_news||{},challenge=item.challenge||{},decision=item.decision||{};return `<tr><td data-label="时间" class="num">${localDateTime(item.asof)}</td><td data-label="股票"><strong>${esc(item.ticker)}</strong><div class="small muted">${esc(item.instrument||"—")}</div></td><td data-label="Bull / News" class="reason">${expandableEvidence(bull.catalyst_summary||"没有保存催化摘要")}</td><td data-label="Challenge" class="reason">${expandableEvidence(firstReason(challenge.objections,challenge.veto_recommended?"建议否决":"未提出关键反对"))}</td><td data-label="Decision" class="reason">${expandableEvidence(decision.thesis||decision.no_trade_reason||"—")}</td><td data-label="Python 风控">${statusBadge(item.risk_approved?"通过":"拒绝",item.risk_approved?"good":"bad")}<div class="small muted">${expandableEvidence(humanReason(item.risk_reason))}</div></td></tr>`}).join("");
  const candidateRows=array(state.candidates).slice().sort((a,b)=>number(b.score)-number(a.score)).slice(0,8).map(item=>`<tr><td data-label="股票"><strong>${esc(item.ticker)}</strong></td><td data-label="综合分" class="num">${(number(item.score)*100).toFixed(1)}</td><td data-label="入场线" class="num">${(number(item.minimum_entry_score)*100).toFixed(1)}</td><td data-label="系统动作">${statusBadge(item.action==="buy"?"候选":"不交易",item.action==="buy"?"good":"warn")}</td><td data-label="原因" class="reason">${esc(firstReason(item.reasons,item.action==="buy"?"达到候选门槛":"暂无原因"))}</td></tr>`).join("");
  const selectedLabel=selected.ticker?`${selected.ticker} · ${selected.instrument_type==="put"?"看跌 Put":selected.instrument_type==="call"?"看涨 Call":"股票"}`:"尚未选择工具";
  document.getElementById("view-ai").className="";
  document.getElementById("view-ai").innerHTML=`<div class="page-heading"><div><h2>AI 决策</h2><p>模型负责结构化研究；只有确定性 Python 风控可以批准模拟执行。</p></div><div class="asof">私有推理原文不会显示</div></div>
    <section class="section-block"><div class="section-head"><div><h3>决策流水线</h3><p>每一步都使用同一 data cutoff，不允许未来数据。</p></div></div><div class="pipeline"><div class="pipeline-step"><strong>1. 候选</strong><span>技术和事件发现</span></div><div class="pipeline-step"><strong>2. Exa 证据</strong><span>新闻与原始来源</span></div><div class="pipeline-step"><strong>3. DeepSeek</strong><span>结构化方向判断</span></div><div class="pipeline-step"><strong>4. Challenge</strong><span>反证与否决建议</span></div><div class="pipeline-step"><strong>5. Python 风控</strong><span>最终 veto 权</span></div></div></section>
    <section class="section-block"><div class="section-head"><div><h3>最近一次工具分配</h3><p>$10,000 独立模拟账户与 $2,000 可负担性对照。</p></div>${statusBadge(allocation.status||"暂无分配",allocation.status==="selected"?"good":"warn")}</div><div class="stat-strip"><div class="stat"><div class="stat-label">选中工具</div><div class="stat-value">${esc(selectedLabel)}</div></div><div class="stat"><div class="stat-label">数量</div><div class="stat-value">${esc(selected.quantity??"—")}</div></div><div class="stat"><div class="stat-label">保守情景估计（非成交 PnL）</div><div class="stat-value ${tone(selected.conservative_net_return_pct)}">${selected.conservative_net_return_pct==null?"—":pct(number(selected.conservative_net_return_pct)*100)}</div></div><div class="stat"><div class="stat-label">概率 EV</div><div class="stat-value">${allocation.probability_ev_available?money(allocation.probability_ev_usd):"未校准，不展示"}</div></div></div></section>
    <section class="section-block"><div class="section-head"><div><h3>Allocator 结构化决策</h3><p>最多显示最近 12 条研究结论。</p></div></div><div class="table-wrap"><table class="mobile-table"><thead><tr><th>时间</th><th>股票</th><th>研究阶段</th><th>方向</th><th>期限</th><th>Challenge</th><th>结构化结论</th></tr></thead><tbody>${decisionRows||'<tr><td colspan="7" class="muted">尚无 allocator 决策。</td></tr>'}</tbody></table></div></section>
    <section class="section-block"><div class="section-head"><div><h3>Exa + DeepSeek 催化研究</h3><p>Bull / News、Challenge、Decision 与确定性 Python 风控分栏展示。</p></div></div><div class="table-wrap"><table class="mobile-table"><thead><tr><th>时间</th><th>股票</th><th>Bull / News</th><th>Challenge</th><th>Decision</th><th>Python 风控</th></tr></thead><tbody>${catalystRows||'<tr><td colspan="6" class="muted">尚无新的催化决策。</td></tr>'}</tbody></table></div></section>
    <section class="section-block"><div class="section-head"><div><h3>最近候选排名</h3><p>这是研究入口，不代表已经创建订单。</p></div></div><div class="table-wrap"><table class="mobile-table"><thead><tr><th>股票</th><th>综合分</th><th>入场线</th><th>系统动作</th><th>原因</th></tr></thead><tbody>${candidateRows||'<tr><td colspan="5" class="muted">尚无候选排名。</td></tr>'}</tbody></table></div></section>`;
}
function healthRows(state){
  const heartbeat=state.heartbeat||{},summary=state.beginner_summary||{},ops=summary.operations||{},news=state.news_drift||{},jobs=((heartbeat.payload||{}).latest_jobs)||{},rows=[];
  const add=(component,status,detail,kind)=>rows.push({component,status,detail,kind});
  const safe=paperOnlyMode(state.mode||{}),mode=state.mode||{};
  add("Paper-only 安全边界",safe?"正常":"异常",`paper=${mode.paper===true} · live_readonly=${mode.live_readonly===true} · live_trading=${mode.live_trading===true}`,safe?"good":"bad");
  const service=heartbeat.effective_status||"unknown";add("Forward service",serviceLabels[service]||service,heartbeat.last_heartbeat_at?`最近心跳 ${localDateTime(heartbeat.last_heartbeat_at)}`:"没有心跳时间",service==="ok"?"good":service==="degraded"?"warn":service==="unknown"?"":"bad");
  const jobValues=Object.values(jobs),failed=jobValues.filter(job=>["failed","timed_out"].includes((job||{}).status)).length;
  if(!jobValues.length){add("Scheduler","无最近状态","heartbeat 中没有受监督作业状态","")}
  else if(service!=="ok"){add("Scheduler","状态已过期",`${jobValues.length} 个作业记录随服务心跳一同过期`,"warn")}
  else{add("Scheduler",failed?`${failed} 个最近作业异常`:"最近作业正常",`${jobValues.length} 个受监督作业有最近状态`,failed?"bad":"good")}
  const addQuote=(label,quote)=>{
    if(!quote.observed_at){add(label,"无报价时间","没有可用的报价观察时间","");return}
    const detail=`最近 ${localDateTime(quote.observed_at)} · ${ageLabel(quote.age_seconds)}${quote.source?" · "+quote.source:""}`;
    add(label,quote.stale?"报价不可用于执行":"时间戳新鲜",detail,quote.stale?"bad":"good");
  };
  const marketData=state.market_data||{};addQuote("股票行情",marketData.equity||{});addQuote("期权行情",marketData.options||{});
  add("Broker 写入边界",safe?"仅行情只读":"模式配置异常",safe?"dashboard 不导入 broker adapter，也没有真实下单方法":`paper=${mode.paper===true} · live_readonly=${mode.live_readonly===true} · live_trading=${mode.live_trading===true}`,safe?"good":"bad");
  add("Exa 新闻发现",(news.latest_cycle||{}).event?"有最近周期":"暂无最近周期",(news.latest_cycle||{}).ts?`最近 ${localDateTime(news.latest_cycle.ts)}`:"等待新的 discovery cycle",(news.latest_cycle||{}).event?"good":"");
  add("DeepSeek 调用",ops.llm_errors?`${ops.llm_errors} 次历史错误`:"最近交易日无记录错误",`${ops.llm_calls||0} 次调用 · 估算成本 $${number(ops.estimated_api_cost_usd).toFixed(4)}`,ops.llm_errors?"warn":"good");
  add("审计与真实下单","只追加 / 未调用",`最近交易日记录 ${ops.runtime_jobs||0} 个作业；dashboard 只读`,"good");
  return rows;
}
function renderHealth(state){
  const b=state.beginner_summary||{},ops=b.operations||{},heartbeat=state.heartbeat||{},jobs=((heartbeat.payload||{}).latest_jobs)||{};
  const componentRows=healthRows(state).map(row=>`<tr><td data-label="组件"><strong>${esc(row.component)}</strong></td><td data-label="状态">${statusBadge(row.status,row.kind)}</td><td data-label="说明" class="reason">${esc(row.detail)}</td></tr>`).join("");
  const jobRows=Object.entries(jobs).map(([name,job])=>`<tr><td data-label="作业"><strong>${esc(name)}</strong></td><td data-label="状态">${statusBadge((job||{}).status||"未知",["failed","timed_out"].includes((job||{}).status)?"bad":(job||{}).status==="completed"?"good":"warn")}</td><td data-label="完成时间" class="num">${localDateTime((job||{}).finished_at||(job||{}).ts)}</td><td data-label="安全摘要" class="reason">${esc(humanReason((job||{}).error||(job||{}).reason||"最近一次作业已保存状态"))}</td></tr>`).join("");
  const issues=array(b.issues),issueRows=issues.map(issue=>`<div class="issue ${issue.severity==="error"?"error":""}"><span class="issue-mark"></span><div><div class="issue-title">${esc(issue.title)}</div><div class="issue-copy">影响：${esc(issue.impact)}<br>处理记录：${esc(issue.resolution)}</div></div><span class="issue-count">${issue.count||0} 次</span></div>`).join("");
  document.getElementById("view-health").className="";
  document.getElementById("view-health").innerHTML=`<div class="page-heading"><div><h2>系统健康</h2><p>当前状态与历史计数分开展示；历史故障不等于此刻仍在失败。</p></div><div class="asof">心跳 ${localDateTime(heartbeat.last_heartbeat_at)}</div></div>
    <section class="section-block"><div class="section-head"><div><h3>当前组件状态</h3><p>这里优先回答现在是否有问题。</p></div></div><div class="table-wrap"><table class="mobile-table"><thead><tr><th>组件</th><th>状态</th><th>说明</th></tr></thead><tbody>${componentRows}</tbody></table></div></section>
    <section class="section-block"><div class="section-head"><div><h3>最近调度作业</h3><p>只显示 heartbeat 中每个作业的最近一次状态。</p></div></div><div class="table-wrap"><table class="mobile-table"><thead><tr><th>作业</th><th>状态</th><th>完成时间</th><th>安全摘要</th></tr></thead><tbody>${jobRows||'<tr><td colspan="4" class="muted">没有可用的最近作业状态。</td></tr>'}</tbody></table></div></section>
    <section class="section-block"><div class="section-head"><div><h3>最近交易日历史事件</h3><p>${esc(b.session_date||"—")} 的累计计数，仅用于复盘，不表示当前仍在故障。</p></div>${statusBadge(issues.length?issues.length+" 类":"无",issues.length?"warn":"good")}</div><div class="issue-list">${issueRows||'<div class="clear-state" style="padding:12px 0">最近交易日没有记录运行异常。</div>'}</div></section>
    <section class="section-block"><div class="stat-strip"><div class="stat"><div class="stat-label">受监督作业</div><div class="stat-value">${ops.runtime_jobs||0}</div></div><div class="stat"><div class="stat-label">历史失败作业</div><div class="stat-value ${ops.failed_jobs?"bad-text":""}">${ops.failed_jobs||0}</div></div><div class="stat"><div class="stat-label">模型调用</div><div class="stat-value">${ops.llm_calls||0}</div></div><div class="stat"><div class="stat-label">估算 API 成本</div><div class="stat-value">$${number(ops.estimated_api_cost_usd).toFixed(4)}</div></div></div></section>`;
}
function activateTab(id,updateHash=true){
  const active=TAB_IDS.includes(id)?id:"overview";
  document.querySelectorAll('[role="tab"]').forEach(tab=>{
    const selected=tab.dataset.tab===active;
    tab.setAttribute("aria-selected",String(selected));
    tab.tabIndex=selected?0:-1;
  });
  TAB_IDS.forEach(tabId=>{document.getElementById(`panel-${tabId}`).hidden=tabId!==active});
  if(updateHash&&location.hash!==`#${active}`)history.replaceState(null,"",`#${active}`);
}
document.querySelectorAll('[role="tab"]').forEach((tab,index)=>{
  tab.addEventListener("click",()=>activateTab(tab.dataset.tab));
  tab.addEventListener("keydown",event=>{
    if(!["ArrowLeft","ArrowRight","Home","End"].includes(event.key))return;
    event.preventDefault();
    let next=index;
    if(event.key==="ArrowLeft")next=(index-1+TAB_IDS.length)%TAB_IDS.length;
    if(event.key==="ArrowRight")next=(index+1)%TAB_IDS.length;
    if(event.key==="Home")next=0;
    if(event.key==="End")next=TAB_IDS.length-1;
    const target=document.getElementById(`tab-${TAB_IDS[next]}`);target.focus();activateTab(TAB_IDS[next]);
  });
});
window.addEventListener("hashchange",()=>activateTab(location.hash.slice(1),false));
activateTab(location.hash.slice(1)||"overview",false);
let refreshInFlight=false;
async function refresh(){
  if(document.hidden||refreshInFlight)return;
  refreshInFlight=true;
  try{
    const response=await fetch("/api/state",{cache:"no-store"});
    if(!response.ok)throw new Error("HTTP "+response.status);
    const state=await response.json();
    renderBoundary(state);renderHeader(state);renderOverview(state);renderPortfolio(state);renderStrategies(state);renderAiDecisions(state);renderHealth(state);
  }catch(error){
    document.getElementById("view-overview").innerHTML=`<div class="error-box"><strong>无法读取本地状态</strong><div>${esc(error.message||error)}</div></div>`;
  }finally{refreshInFlight=false}
}
document.addEventListener("visibilitychange",()=>{if(!document.hidden)refresh()});
refresh();setInterval(refresh,REFRESH_INTERVAL_MS);
</script>
</body>
</html>"""


def make_handler(root: Path) -> type[BaseHTTPRequestHandler]:
    class DashboardHandler(BaseHTTPRequestHandler):
        def do_HEAD(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path == "/api/state":
                status = HTTPStatus.OK
                content_type = "application/json; charset=utf-8"
            elif path == "/":
                status = HTTPStatus.OK
                content_type = "text/html; charset=utf-8"
            elif path == "/favicon.ico":
                status = HTTPStatus.NO_CONTENT
                content_type = "image/x-icon"
            else:
                status = HTTPStatus.NOT_FOUND
                content_type = "text/plain; charset=utf-8"
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

        def do_OPTIONS(self) -> None:  # noqa: N802
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_header("Allow", "GET, HEAD, OPTIONS")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path == "/api/state":
                body = json.dumps(build_dashboard_state(root), ensure_ascii=False).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json; charset=utf-8")
            elif path == "/":
                body = _BEGINNER_PAGE.encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
            elif path == "/favicon.ico":
                body = b""
                self.send_response(HTTPStatus.NO_CONTENT)
                self.send_header("Content-Type", "image/x-icon")
            else:
                body = b"Not found"
                self.send_response(HTTPStatus.NOT_FOUND)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                # Browser refreshes can be cancelled while state is rendering.
                return

        def log_message(self, _format: str, *_args: Any) -> None:
            return

    return DashboardHandler


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the read-only paper-trading dashboard.")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()
    root = Path(args.root).resolve()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(root))
    print(f"Paper Trading Control Room: http://{args.host}:{args.port}", flush=True)
    print("Read-only dashboard; Ctrl+C stops only this dashboard server.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
