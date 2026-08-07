"""Local, read-only GUI for the paper/shadow trading audit trail.

This server never imports a broker adapter and exposes no mutation endpoint.
It is deliberately separate from the scheduler so it can inspect a live service
without competing for its process lock or current-user OAuth credentials.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from scripts.core.config import load_runtime_config
from scripts.core.models import parse_ts, utc_now
from scripts.runtime.process_lock import ProcessLock
from scripts.evaluation.calculate_metrics import calculate_metrics
from scripts.evaluation.evaluate_news_drift import calculate_news_drift_metrics


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _read_jsonl(path: Path, limit: int = 400) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in lines[-limit:]:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            # The service can be appending while the dashboard reads. Ignore a
            # single incomplete line and retry on the next browser refresh.
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


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
    session_date = str(counters.get("date") or "")
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
    regular_clock = (
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
    current_session = (
        (
            heartbeat.get("payload", {})
            .get("latest_jobs", {})
            .get("forward", {})
            .get("output")
            or {}
        )
        .get("clock", {})
        .get("market_session")
    )
    if current_session is None:
        latest_clock_record = _last(
            session_audit,
            lambda record: isinstance(record.get("clock"), dict),
        )
        current_session = (
            (latest_clock_record or {}).get("clock", {}).get("market_session")
        )

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
    audit_records = _read_jsonl(logs_dir / "audit.jsonl", limit=2000)
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
    orders = list(orders_raw.values()) if isinstance(orders_raw, dict) else []
    orders.sort(key=lambda item: str(item.get("submitted_at") or item.get("created_at") or ""), reverse=True)
    for order in orders:
        snapshot_id = str(order.get("decision_id", ""))
        order["baseline_explanation"] = decisions_by_snapshot.get(snapshot_id)
        order["shadow_explanation"] = shadow_by_snapshot.get(snapshot_id)

    positions_raw = _read_json(state_dir / "paper_positions.json", {})
    positions = list(positions_raw.values()) if isinstance(positions_raw, dict) else []
    for position in positions:
        position["exit_evaluation"] = exit_by_symbol.get(str(position.get("symbol", "")))

    option_orders_raw = _read_json(state_dir / "paper_option_orders.json", {})
    option_orders = list(option_orders_raw.values()) if isinstance(option_orders_raw, dict) else []
    option_orders.sort(key=lambda item: str(item.get("submitted_at") or item.get("created_at") or ""), reverse=True)
    option_positions_raw = _read_json(state_dir / "paper_option_positions.json", {})
    option_positions = list(option_positions_raw.values()) if isinstance(option_positions_raw, dict) else []

    latest_cycle = _last(audit_records, lambda item: item.get("event") in {"forward_cycle_complete", "forward_cycle_exit_only", "forward_cycle_skipped", "forward_cycle_failed_closed"})
    last_shadow = _safe_shadow_record(shadow_records[-1]) if shadow_records else None
    paper = runtime["paper"]
    risk = runtime["risk"]
    thinking = runtime["llm"].get("api", {}).get("thinking")
    heartbeat = _read_json(state_dir / "runtime_heartbeat.json", {})
    heartbeat_age = None
    if heartbeat.get("last_heartbeat_at"):
        heartbeat_age = max(
            0.0,
            (parse_ts(utc_now()) - parse_ts(str(heartbeat["last_heartbeat_at"]))).total_seconds(),
        )
    stale_after = int(runtime.get("integrations", {}).get("runtime", {}).get("watchdog_max_age_seconds", 900))
    heartbeat["age_seconds"] = round(heartbeat_age, 1) if heartbeat_age is not None else None
    heartbeat["stale"] = heartbeat_age is None or heartbeat_age > stale_after
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
    account = _read_json(state_dir / "paper_account.json", {})
    counters = _read_json(state_dir / "daily_counters.json", {})
    metrics = calculate_metrics(root_path)
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
    return {
        "mode": {"paper": bool(paper.get("mode", {}).get("paper", False)), "live_trading": bool(paper.get("mode", {}).get("live_trading", False))},
        "heartbeat": heartbeat,
        "account": account,
        "daily_counters": counters,
        "positions": positions,
        "option_positions": option_positions,
        "orders": orders[:30],
        "option_orders": option_orders[:30],
        "latest_cycle": latest_cycle,
        "candidates": candidates,
        "option_decisions": option_decisions,
        "metrics": metrics,
        "beginner_summary": beginner_summary,
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
            "positions": list(_read_json(ai_state_dir / "paper_positions.json", {}).values()) if ai_account_exists else [],
            "option_positions": list(_read_json(ai_state_dir / "paper_option_positions.json", {}).values()) if ai_account_exists else [],
            "orders": list(ai_orders_raw.values()) if isinstance(ai_orders_raw, dict) else [],
            "option_orders": list(ai_option_orders_raw.values()) if isinstance(ai_option_orders_raw, dict) else [],
            "metrics": calculate_metrics(root_path, namespace=ai_namespace) if ai_account_exists else None,
            "latest_cycle": ai_cycle_records[-1] if ai_cycle_records else None,
            "decisions": [_safe_ai_record(record) for record in ai_decision_records[-20:]],
        },
        "news_drift": {
            "metrics": calculate_news_drift_metrics(root_path),
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
  const eqCopy=eq.entries?`监控 ${eq.watchlist_count||0} 个标的；开仓 ${eq.entries||0} 笔，平仓 ${eq.closed_trades||0} 笔。${eq.earnings_risk_entries?`其中 ${eq.earnings_risk_entries} 笔发生在临近财报的标的，已新增财报风险门。`:"系统会继续按加权分数和确定性风控筛选。"}`:`监控 ${eq.watchlist_count||0} 个标的；这一天没有新开股票仓位。`;
  const opCopy=op.status==="validation_error"?`完成 ${op.direction_evaluations||0} 次方向评估；进入合约筛选 ${op.selection_attempts||0} 次，但报价时间校验故障使合约全部落选。`:`完成 ${op.direction_evaluations||0} 次方向评估；进入合约筛选 ${op.selection_attempts||0} 次，未创建模拟期权订单。`;
  const aiCopy=ai.status==="failed_closed"?`${ai.failed||0} 次模型排名失败，系统安全停止，没有下单。`:`完成 ${ai.completed||0} 次 AI 决策循环；最近一次从 ${ai.latest_candidate_count||0} 个候选中选出 ${ai.latest_top_set_count||0} 个做新闻研究。`;
  return `<section class="band"><div class="band-head"><div><h2>三条策略线分别发生了什么</h2><p class="muted">股票、期权和 AI 独立统计，但共享总账户风险上限。</p></div></div><div class="band-body"><div class="strategy-grid">
    <article class="strategy"><div class="strategy-top"><h3>股票加权策略</h3><span class="pill ${eq.status==="traded"?"good":""}">${esc(statusLabel[eq.status]||eq.status)}</span></div><div class="strategy-number ${tone(eq.daily_pnl)}">${signedMoney(eq.daily_pnl)}</div><p class="strategy-copy">${esc(eqCopy)}</p></article>
    <article class="strategy"><div class="strategy-top"><h3>买入 Call / Put 期权</h3><span class="pill ${op.status==="validation_error"?"bad":""}">${esc(statusLabel[op.status]||op.status)}</span></div><div class="strategy-number">${op.orders||0} 笔订单</div><p class="strategy-copy">${esc(opCopy)}</p></article>
    <article class="strategy"><div class="strategy-top"><h3>AI 独立模拟策略</h3><span class="pill ${ai.status==="failed_closed"?"bad":"good"}">${esc(statusLabel[ai.status]||ai.status)}</span></div><div class="strategy-number">${ai.completed||0} 次完成</div><p class="strategy-copy">${esc(aiCopy)}</p></article>
  </div></div></section>`;
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
  return `<section class="band"><div class="band-head"><div><h2>最后一次股票筛选</h2><p class="muted">这里只显示分数最高的 8 个；实际监控数量见上方股票策略。分数高不代表一定上涨，风险门仍可否决。</p></div></div><div class="table-wrap"><table class="mobile-stack"><thead><tr><th>股票</th><th>系统动作</th><th>综合分</th><th>简单原因</th></tr></thead><tbody>${rows||'<tr><td colspan="4" class="muted">暂无股票筛选记录。</td></tr>'}</tbody></table></div></section>`;
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
  document.getElementById("app").innerHTML=overview(d)+issues(d)+tradeTable(d)+strategies(d)+newsDrift(d)+candidates(d)+advanced(d);
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


def make_handler(root: Path) -> type[BaseHTTPRequestHandler]:
    class DashboardHandler(BaseHTTPRequestHandler):
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
            self.wfile.write(body)

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
