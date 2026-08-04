from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


CATALYST_AGENTS = {
    "catalyst_candidate_extractor",
    "catalyst_ranker",
    "catalyst_bull_news_agent",
    "catalyst_challenge_agent",
    "catalyst_decision_manager",
}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def evaluate_catalyst_strategy(root: str | Path) -> dict[str, Any]:
    root = Path(root)
    cycles = [
        item
        for item in _read_jsonl(root / "logs" / "catalyst_discovery.jsonl")
        if item.get("event") == "catalyst_discovery_complete"
    ]
    decisions = _read_jsonl(root / "logs" / "catalyst_decisions.jsonl")
    usage = [
        item
        for item in _read_jsonl(root / "logs" / "llm_usage.jsonl")
        if item.get("agent_name") in CATALYST_AGENTS
    ]
    final_no_trade = sum(1 for item in decisions if item.get("final_action") == "no_trade")
    risk_approved = sum(1 for item in decisions if item.get("risk_approved") is True)
    instruments = {
        name: sum(1 for item in decisions if item.get("instrument") == name)
        for name in ("equity", "call", "put", "none")
    }
    model_errors = sum(1 for item in usage if item.get("error"))
    fail_closed = sum(1 for item in decisions if item.get("fail_closed"))
    candidate_total = sum(int(item.get("candidate_count", 0)) for item in cycles)
    report = {
        "strategy": "exa_deepseek_catalyst_v1",
        "execution": "shadow_only",
        "discovery_cycles": len(cycles),
        "candidate_count": candidate_total,
        "average_candidates_per_cycle": round(candidate_total / len(cycles), 3) if cycles else 0.0,
        "deep_decision_count": len(decisions),
        "no_trade_rate": round(final_no_trade / len(decisions), 4) if decisions else 0.0,
        "risk_approval_rate": round(risk_approved / len(decisions), 4) if decisions else 0.0,
        "instrument_proposals": instruments,
        "fail_closed_count": fail_closed,
        "model_calls": len(usage),
        "model_errors": model_errors,
        "input_tokens": sum(int(item.get("input_tokens", 0)) for item in usage),
        "output_tokens": sum(int(item.get("output_tokens", 0)) for item in usage),
        "latency_ms": round(sum(float(item.get("latency_ms", 0)) for item in usage), 3),
        "estimated_cost_usd": round(sum(float(item.get("estimated_cost_usd") or 0) for item in usage), 8),
        "paper_orders_created": sum(int(item.get("paper_orders_created", 0)) for item in cycles),
        "profitability": "not_applicable_shadow_only",
        "promotion_eligible": False,
        "data_gaps": [
            "Shadow proposals have no independent fills or realized PnL.",
            "Forward outcome labeling is required before precision or return can be estimated.",
        ],
    }
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[2]))
    args = parser.parse_args()
    print(json.dumps(evaluate_catalyst_strategy(args.root), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
