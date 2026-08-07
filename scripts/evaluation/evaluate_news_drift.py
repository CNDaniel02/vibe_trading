from __future__ import annotations

import argparse
import json
import math
import sqlite3
from collections import defaultdict
from pathlib import Path
from statistics import fmean
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from scripts.core.config import load_runtime_config
from scripts.core.models import parse_ts, utc_now


NEW_YORK = ZoneInfo("America/New_York")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    result: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            result.append(value)
    return result


def _returns(values: Iterable[float]) -> dict[str, Any]:
    samples = [float(value) for value in values]
    gains = sum(value for value in samples if value > 0)
    losses = abs(sum(value for value in samples if value < 0))
    profit_factor = gains / losses if losses else (math.inf if gains else 0.0)
    return {
        "count": len(samples),
        "mean_return_pct": round(fmean(samples), 6) if samples else None,
        "hit_rate": round(sum(value > 0 for value in samples) / len(samples), 6) if samples else None,
        "profit_factor": round(profit_factor, 6) if math.isfinite(profit_factor) else "infinity",
    }


def _bucket(rows: list[dict[str, Any]], field: str) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        value = row.get(field)
        grouped[str(value if value not in (None, "") else "unknown")].append(row["net_return_pct"])
    return {key: _returns(values) for key, values in sorted(grouped.items())}


def _size_bucket(value: Any) -> str:
    try:
        market_cap = float(value)
    except (TypeError, ValueError):
        return "unknown"
    if market_cap < 2_000_000_000:
        return "small_1b_2b"
    if market_cap < 10_000_000_000:
        return "mid_2b_10b"
    return "large_10b_plus"


def calculate_news_drift_metrics(root: str | Path) -> dict[str, Any]:
    root = Path(root)
    database = root / "state" / "news_events.sqlite"
    if not database.is_file():
        return {
            "strategy": "llm_news_drift_v1",
            "execution": "shadow_only",
            "status": "no_forward_data",
            "promotion_eligible": False,
            "profitability": "insufficient_forward_evidence",
            "event_count": 0,
            "signal_count": 0,
            "proposal_count": 0,
            "label_count": 0,
        }
    config = (
        load_runtime_config(root)
        if (root / "config" / "paper_mode.yaml").is_file()
        else {"strategies": {}}
    )
    profile = config.get("strategies", {}).get("llm_news_drift_v1", {})

    connection = sqlite3.connect(str(database), timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        counts = {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in (
                "news_events",
                "event_relations",
                "llm_signals",
                "tradability_observations",
                "shadow_proposals",
                "outcome_labels",
            )
        }
        raw_rows = connection.execute(
            """
            SELECT l.*, p.ticker, p.entry_price,
                   p.payload_json AS proposal_payload_json,
                   s.payload_json AS signal_payload_json,
                   e.published_at, e.first_seen_at, e.source_tier
            FROM outcome_labels AS l
            JOIN shadow_proposals AS p ON p.proposal_id = l.proposal_id
            JOIN llm_signals AS s ON s.signal_id = l.signal_id
            JOIN news_events AS e ON e.event_id = l.event_id
            ORDER BY l.outcome_time
            """
        ).fetchall()
    finally:
        connection.close()

    rows: list[dict[str, Any]] = []
    for raw in raw_rows:
        if raw["return_pct"] is None:
            continue
        proposal = json.loads(raw["proposal_payload_json"])
        signal = json.loads(raw["signal_payload_json"])
        label = json.loads(raw["payload_json"])
        published_day = parse_ts(str(raw["published_at"])).astimezone(NEW_YORK).date().isoformat()
        rows.append(
            {
                "proposal_id": str(raw["proposal_id"]),
                "ticker": str(raw["ticker"]),
                "firm_day": f"{raw['ticker']}:{published_day}",
                "portfolio_day": published_day,
                "horizon": str(raw["horizon"]),
                "net_return_pct": float(raw["return_pct"]),
                "gross_return_pct": float(label.get("gross_return_pct", raw["return_pct"])),
                "event_type": signal.get("event_type"),
                "direction": signal.get("direction"),
                "source_tier": raw["source_tier"],
                "market_cap_bucket": _size_bucket(proposal.get("market_cap_usd")),
                "observation_delay_seconds": label.get("observation_delay_seconds"),
            }
        )

    horizon_metrics: dict[str, dict[str, Any]] = {}
    firm_day_rows: list[dict[str, Any]] = []
    portfolio_day_rows: list[dict[str, Any]] = []
    for horizon in sorted({row["horizon"] for row in rows}):
        horizon_rows = [row for row in rows if row["horizon"] == horizon]
        firm_groups: dict[str, list[float]] = defaultdict(list)
        firm_gross: dict[str, list[float]] = defaultdict(list)
        firm_dates: dict[str, str] = {}
        for row in horizon_rows:
            firm_groups[row["firm_day"]].append(row["net_return_pct"])
            firm_gross[row["firm_day"]].append(row["gross_return_pct"])
            firm_dates[row["firm_day"]] = row["portfolio_day"]
        horizon_firm = [
            {
                "firm_day": key,
                "portfolio_day": firm_dates[key],
                "horizon": horizon,
                "net_return_pct": fmean(values),
                "gross_return_pct": fmean(firm_gross[key]),
            }
            for key, values in firm_groups.items()
        ]
        firm_day_rows.extend(horizon_firm)
        portfolio_groups: dict[str, list[float]] = defaultdict(list)
        for row in horizon_firm:
            portfolio_groups[row["portfolio_day"]].append(row["net_return_pct"])
        horizon_portfolio = [
            {
                "portfolio_day": day,
                "horizon": horizon,
                "net_return_pct": fmean(values),
            }
            for day, values in portfolio_groups.items()
        ]
        portfolio_day_rows.extend(horizon_portfolio)
        gross = [row["gross_return_pct"] for row in horizon_rows]
        net = [row["net_return_pct"] for row in horizon_rows]
        horizon_metrics[horizon] = {
            "event_level": _returns(net),
            "firm_day": _returns(row["net_return_pct"] for row in horizon_firm),
            "portfolio_day": _returns(row["net_return_pct"] for row in horizon_portfolio),
            "gross": _returns(gross),
            "observed_round_trip_cost_bps": round(
                fmean((gross_value - net_value) * 100 for gross_value, net_value in zip(gross, net)), 6
            )
            if net
            else None,
            "break_even_total_cost_bps": round(max(0.0, fmean(gross) * 100), 6) if gross else None,
            "cost_sensitivity": {
                str(cost_bps): _returns(value - cost_bps / 100 for value in gross)
                for cost_bps in (0, 5, 10, 25, 50)
            },
        }

    usage = [
        item
        for item in _read_jsonl(root / "logs" / "llm_usage.jsonl")
        if item.get("agent_name") == "news_drift_headline_agent"
        and str(item.get("snapshot_id", "")).startswith("ndr_")
    ]
    cycles = [
        item
        for item in _read_jsonl(root / "logs" / "news_drift_cycles.jsonl")
        if item.get("event") in {"news_drift_complete", "news_drift_failed_closed"}
    ]
    portfolio_days = len({row["portfolio_day"] for row in portfolio_day_rows})
    minimum_labels = int(profile.get("minimum_event_labels_for_evaluation", 100))
    minimum_days = int(profile.get("minimum_portfolio_days_for_evaluation", 20))
    sufficient = len(rows) >= minimum_labels and portfolio_days >= minimum_days
    next_close = horizon_metrics.get("next_close", {}).get("portfolio_day", {})
    profitable = bool(
        sufficient
        and next_close.get("mean_return_pct") is not None
        and float(next_close["mean_return_pct"]) > 0
        and next_close.get("profit_factor") not in (None, 0, 0.0)
        and (
            next_close.get("profit_factor") == "infinity"
            or float(next_close["profit_factor"]) >= 1.2
        )
    )
    return {
        "strategy": "llm_news_drift_v1",
        "execution": "shadow_only",
        "evaluated_at": utc_now(),
        "status": "forward_evaluation",
        "event_count": counts["news_events"],
        "relation_count": counts["event_relations"],
        "signal_count": counts["llm_signals"],
        "tradability_observation_count": counts["tradability_observations"],
        "proposal_count": counts["shadow_proposals"],
        "label_count": counts["outcome_labels"],
        "valid_return_label_count": len(rows),
        "firm_day_count": len({row["firm_day"] + ":" + row["horizon"] for row in firm_day_rows}),
        "portfolio_day_count": portfolio_days,
        "horizons": horizon_metrics,
        "buckets": {
            "event_type": _bucket(rows, "event_type"),
            "direction": _bucket(rows, "direction"),
            "source_tier": _bucket(rows, "source_tier"),
            "market_cap": _bucket(rows, "market_cap_bucket"),
        },
        "api_usage": {
            "llm_calls": len(usage),
            "input_tokens": sum(int(item.get("input_tokens", 0)) for item in usage),
            "output_tokens": sum(int(item.get("output_tokens", 0)) for item in usage),
            "estimated_llm_cost_usd": round(sum(float(item.get("estimated_cost_usd") or 0) for item in usage), 8),
            "exa_search_cycles": sum(1 for item in cycles if item.get("query")),
            "estimated_exa_cost_usd": None,
            "unpriced_exa_searches": sum(1 for item in cycles if item.get("query")),
        },
        "evidence_sufficient": sufficient,
        "evaluation_thresholds": {
            "minimum_event_labels": minimum_labels,
            "minimum_portfolio_days": minimum_days,
            "primary_profitability_horizon": "next_close",
        },
        "promotion_eligible": False,
        "profitability": (
            "profitable_shadow_candidate"
            if profitable
            else ("insufficient_forward_evidence" if not sufficient else "not_profitable_after_costs")
        ),
        "isolation": {
            "paper_orders_created": 0,
            "live_order_tools_called": False,
            "shares_main_account": False,
        },
    }


def generate_news_drift_report(root: str | Path) -> Path:
    root = Path(root)
    metrics = calculate_news_drift_metrics(root)
    path = root / "logs" / "news_drift_performance_report.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    next_close = metrics.get("horizons", {}).get("next_close", {})
    next_portfolio = next_close.get("portfolio_day", {})
    lines = [
        "# LLM News Drift Shadow Report",
        "",
        f"- Evaluated at: {metrics.get('evaluated_at', 'not available')}",
        f"- Events / signals / proposals: {metrics.get('event_count', 0)} / {metrics.get('signal_count', 0)} / {metrics.get('proposal_count', 0)}",
        f"- Valid labels / portfolio days: {metrics.get('valid_return_label_count', 0)} / {metrics.get('portfolio_day_count', 0)}",
        f"- Next-close portfolio-day mean return: {next_portfolio.get('mean_return_pct')}",
        f"- Next-close portfolio-day hit rate: {next_portfolio.get('hit_rate')}",
        f"- Forward evidence sufficient: {metrics.get('evidence_sufficient', False)}",
        f"- Profitability: **{metrics.get('profitability')}**",
        f"- Promotion eligible: **{metrics.get('promotion_eligible', False)}**",
        "",
        "The strategy remains shadow-only. Gross, spread/slippage-adjusted net, firm-day, and portfolio-day results are kept separately for every horizon.",
        "Exa search cost remains unpriced until a per-search price is configured; it is not silently treated as zero.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[2]))
    args = parser.parse_args()
    report = generate_news_drift_report(args.root)
    print(json.dumps({"metrics": calculate_news_drift_metrics(args.root), "report_path": str(report)}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
