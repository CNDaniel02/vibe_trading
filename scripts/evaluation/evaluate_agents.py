from __future__ import annotations

import argparse
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from scripts.agents.api_investment_team import ApiInvestmentTeam
from scripts.agents.deterministic_agents import quote_from_snapshot
from scripts.core.config import assert_paper_mode, load_runtime_config
from scripts.core.models import Account, Order
from scripts.llm import build_provider
from scripts.llm.schemas import CHALLENGE_OUTPUT_SCHEMA, DECISION_OUTPUT_SCHEMA, NEWS_OUTPUT_SCHEMA, validate_agent_input, validate_schema
from scripts.risk.risk_gate import check_order
from scripts.simulation.fill_model import simulate_fill
from scripts.strategies.relative_strength_v1 import decide_snapshot


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FIXTURES = PROJECT_ROOT / "fixtures" / "agent_snapshots" / "snapshots.json"


def deep_merge(base: Any, override: Any) -> Any:
    if isinstance(base, dict) and isinstance(override, dict):
        result = deepcopy(base)
        for key, value in override.items():
            result[key] = deep_merge(result[key], value) if key in result else deepcopy(value)
        return result
    return deepcopy(override)


def load_snapshots(path: str | Path = DEFAULT_FIXTURES) -> list[dict[str, Any]]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    defaults = raw["defaults"]
    return [deep_merge(defaults, case) for case in raw["cases"]]


def agent_input(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {key: deepcopy(value) for key, value in snapshot.items() if key != "expected"}


def _decision_return(actions: list[str], snapshots: list[dict[str, Any]]) -> tuple[float, float, float | None]:
    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    gains = 0.0
    losses = 0.0
    for action, snapshot in zip(actions, snapshots):
        if action != "buy":
            continue
        trade_return = float(snapshot["expected"].get("forward_return_pct", 0)) / 100
        pnl = 0.25 * trade_return
        equity *= 1 + pnl
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, (peak - equity) / peak)
        if pnl >= 0:
            gains += pnl
        else:
            losses += abs(pnl)
    profit_factor = gains / losses if losses else None
    return (equity - 1) * 100, max_drawdown * 100, profit_factor


def _precision(actions: list[str], snapshots: list[dict[str, Any]]) -> float:
    selected = [snapshot for action, snapshot in zip(actions, snapshots) if action == "buy"]
    if not selected:
        return 0.0
    winners = sum(float(item["expected"].get("forward_return_pct", 0)) > 0 for item in selected)
    return winners / len(selected)


def _execution_outcomes(actions: list[str], snapshots: list[dict[str, Any]], config: dict[str, Any]) -> tuple[list[str], float, int]:
    effective: list[str] = []
    submitted = 0
    unfilled = 0
    rejected = 0
    for action, snapshot in zip(actions, snapshots):
        if action != "buy":
            effective.append(action)
            continue
        quote = quote_from_snapshot(snapshot)
        if quote is None:
            effective.append("no_trade")
            rejected += 1
            continue
        order = Order(
            order_id=f"eval_{snapshot['snapshot_id']}",
            decision_id=f"eval_{snapshot['snapshot_id']}",
            symbol=snapshot["ticker"],
            side="buy",
            order_type="limit",
            quantity=1.0,
            limit_price=quote.ask,
            quote_seen_at=quote.asof,
            idempotency_key=f"eval_{snapshot['snapshot_id']}",
            created_at=snapshot["decision_time"],
        )
        account = Account(cash=2000, initial_cash=2000, updated_at=snapshot["decision_time"])
        risk = check_order(order, quote, account, {}, {}, {"date": snapshot["decision_time"][:10], "trades": 0}, config, snapshot["decision_time"])
        if not risk.approved:
            effective.append("no_trade")
            rejected += 1
            continue
        submitted += 1
        fill = simulate_fill(order, quote, config["costs"], filled_at=snapshot["decision_time"])
        if fill.status != "filled":
            effective.append("no_trade")
            unfilled += 1
        else:
            effective.append("buy")
    return effective, (unfilled / submitted if submitted else 0.0), rejected


def evaluate(
    root: str | Path = PROJECT_ROOT,
    fixtures: str | Path = DEFAULT_FIXTURES,
    provider_name: str = "mock",
    limit: int | None = None,
) -> dict[str, Any]:
    root = Path(root)
    config = load_runtime_config(root)
    assert_paper_mode(config)
    config["llm"] = deepcopy(config.get("llm", {}))
    config["llm"]["provider"] = provider_name
    config["llm"]["usage_log"] = f"logs/evals/{provider_name}_usage.jsonl"
    provider, tracker = build_provider(config["llm"], root)
    team = ApiInvestmentTeam(root, config, provider, tracker)
    snapshots = load_snapshots(fixtures)
    if limit is not None:
        snapshots = snapshots[:limit]

    results = []
    schema_valid = 0
    timestamp_correct = 0
    grounded_events = 0
    total_events = 0
    hallucinated_events = 0
    expected_matches = 0
    challenge_expected = 0
    challenge_useful = 0
    rule_violations = 0

    for snapshot in snapshots:
        result = team.run(agent_input(snapshot))
        results.append(result.to_dict())
        try:
            validate_schema(result.decision, DECISION_OUTPUT_SCHEMA)
            if result.news is not None:
                validate_schema(result.news, NEWS_OUTPUT_SCHEMA)
            if result.challenge is not None:
                validate_schema(result.challenge, CHALLENGE_OUTPUT_SCHEMA)
            schema_valid += 1
        except Exception:
            pass

        invalid_timestamp = bool(snapshot["expected"].get("invalid_timestamp", False))
        try:
            validate_agent_input(agent_input(snapshot))
            timestamp_correct += int(not invalid_timestamp and not result.fail_closed)
        except Exception:
            timestamp_correct += int(invalid_timestamp and result.fail_closed and result.model_calls == 0)

        input_events = {
            (item.get("headline"), item.get("source"), item.get("published_at"), item.get("first_seen_at"))
            for item in snapshot["available_news"]
        }
        for event in (result.news or {}).get("events", []):
            total_events += 1
            key = (event.get("headline"), event.get("source"), event.get("published_at"), event.get("first_seen_at"))
            if key in input_events:
                grounded_events += 1
            else:
                hallucinated_events += 1

        expected_matches += int(result.action == snapshot["expected"]["action"])
        if snapshot["expected"].get("challenge_veto"):
            challenge_expected += 1
            challenge = result.challenge or {}
            useful_detail = any(challenge.get(key) for key in ("objections", "contradictions", "missing_evidence", "stale_evidence"))
            challenge_useful += int(bool(challenge.get("veto_recommended")) and useful_detail)

        forbidden = {"order", "order_id", "quantity", "position_size", "place_order"}
        if forbidden.intersection(result.decision):
            rule_violations += 1
        if result.action == "buy" and not result.risk_approved:
            rule_violations += 1
        if result.model_calls > 3:
            rule_violations += 1

    second_provider, second_tracker = build_provider({**config["llm"], "provider": "mock", "usage_log": "logs/evals/mock_consistency_usage.jsonl"}, root)
    second_team = ApiInvestmentTeam(root, config, second_provider, second_tracker)
    consistent = 0
    if provider_name == "mock":
        for snapshot, first in zip(snapshots, results):
            second = second_team.run(agent_input(snapshot)).to_dict()
            consistent += int(first == second)

    v2_actions = [item["action"] for item in results]
    v2_effective_actions, shadow_unfilled, _ = _execution_outcomes(v2_actions, snapshots, config)
    baseline_results = [decide_snapshot(snapshot, config) for snapshot in snapshots]
    baseline_signals = [item["action"] for item in baseline_results]
    baseline_actions, baseline_unfilled, baseline_risk_rejections = _execution_outcomes(baseline_signals, snapshots, config)
    v2_return, v2_drawdown, v2_pf = _decision_return(v2_effective_actions, snapshots)
    v1_return, v1_drawdown, v1_pf = _decision_return(baseline_actions, snapshots)
    comparison = {
        "decision_agreement": sum(a == b for a, b in zip(baseline_actions, v2_effective_actions)) / len(snapshots),
        "baseline": {
            "strategy": "relative_strength_v1",
            "no_trade_rate": baseline_actions.count("no_trade") / len(snapshots),
            "candidate_precision": _precision(baseline_actions, snapshots),
            "net_return_pct": round(v1_return, 4),
            "max_drawdown_pct": round(v1_drawdown, 4),
            "profit_factor": v1_pf,
            "unfilled_rate": baseline_unfilled,
            "risk_rejections": baseline_risk_rejections,
            "decision_latency_ms": 0.0,
            "api_cost_usd": 0.0,
            "rule_violations": 0,
        },
        "shadow": {
            "strategy": "multi_agent_relative_strength_v2_candidate",
            "no_trade_rate": v2_effective_actions.count("no_trade") / len(snapshots),
            "candidate_precision": _precision(v2_effective_actions, snapshots),
            "net_return_pct": round(v2_return, 4),
            "max_drawdown_pct": round(v2_drawdown, 4),
            "profit_factor": v2_pf,
            "unfilled_rate": shadow_unfilled,
            "risk_rejections": sum("deterministic risk gate vetoed model decision" in item["guardrail_actions"] for item in results),
            "decision_latency_ms": round(tracker.summary()["latency_ms"] / len(snapshots), 3),
            "api_cost_usd": tracker.summary()["estimated_cost_usd"],
            "rule_violations": rule_violations,
        },
        "note": "Return metrics use fixed fixture forward returns and 25% notional per buy; they are eval diagnostics, not evidence of strategy profitability.",
    }
    count = len(snapshots)
    return {
        "provider": provider_name,
        "snapshot_count": count,
        "scores": {
            "schema_validity": schema_valid / count,
            "timestamp_correctness": timestamp_correct / count,
            "source_grounding": grounded_events / total_events if total_events else 1.0,
            "hallucination_rate": hallucinated_events / total_events if total_events else 0.0,
            "no_trade_correctness": expected_matches / count,
            "challenge_usefulness": challenge_useful / challenge_expected if challenge_expected else 1.0,
            "decision_consistency": consistent / count if provider_name == "mock" else None,
            "risk_rule_compliance": 1 - (rule_violations / count),
        },
        "usage": tracker.summary(),
        "comparison": comparison,
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=str(PROJECT_ROOT))
    parser.add_argument("--fixtures", default=str(DEFAULT_FIXTURES))
    parser.add_argument("--provider", choices=["mock", "api", "local"], default="mock")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--output")
    args = parser.parse_args()
    report = evaluate(args.root, args.fixtures, args.provider, args.limit)
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
