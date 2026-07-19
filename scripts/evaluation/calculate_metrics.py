from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from scripts.core.config import load_runtime_config
from scripts.core.models import Account, Order, Position


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            records.append(item)
    return records


def _closed_trade_pnls(fill_records: list[dict[str, Any]]) -> list[float]:
    holdings: dict[str, dict[str, float]] = {}
    pnls: list[float] = []
    fills = [record.get("fill", record) for record in fill_records]
    fills.sort(key=lambda fill: str(fill.get("filled_at", "")))
    for fill in fills:
        symbol = str(fill.get("symbol", ""))
        quantity = float(fill.get("quantity", 0))
        price = float(fill.get("price", 0))
        commission = float(fill.get("commission", 0))
        if not symbol or quantity <= 0 or price <= 0:
            continue
        holding = holdings.setdefault(symbol, {"quantity": 0.0, "average_price": 0.0})
        if fill.get("side") == "buy":
            new_quantity = holding["quantity"] + quantity
            holding["average_price"] = (
                holding["quantity"] * holding["average_price"] + quantity * price + commission
            ) / new_quantity
            holding["quantity"] = new_quantity
        elif fill.get("side") == "sell" and holding["quantity"] + 1e-9 >= quantity:
            pnl = quantity * (price - holding["average_price"]) - commission
            pnls.append(pnl)
            holding["quantity"] -= quantity
            if holding["quantity"] <= 1e-9:
                holdings.pop(symbol, None)
    return pnls


def _max_drawdown_pct(equities: list[float]) -> float:
    peak = 0.0
    maximum = 0.0
    for equity in equities:
        peak = max(peak, equity)
        if peak > 0:
            maximum = max(maximum, (peak - equity) / peak * 100)
    return maximum


def calculate_metrics(root: str | Path) -> dict[str, Any]:
    root = Path(root)
    config = load_runtime_config(root)
    account = Account.from_dict(json.loads((root / "state" / "paper_account.json").read_text(encoding="utf-8")))
    positions = {
        symbol: Position.from_dict(value)
        for symbol, value in json.loads((root / "state" / "paper_positions.json").read_text(encoding="utf-8")).items()
    }
    orders = {
        order_id: Order.from_dict(value)
        for order_id, value in json.loads((root / "state" / "paper_orders.json").read_text(encoding="utf-8")).items()
    }
    snapshots = _read_jsonl(root / "logs" / "portfolio_snapshots.jsonl")
    valid_snapshots = [item for item in snapshots if item.get("equity") is not None]
    equities = [float(item["equity"]) for item in valid_snapshots]
    sessions = {str(item.get("session")) for item in valid_snapshots if item.get("session")}
    closed_pnls = _closed_trade_pnls(_read_jsonl(root / "logs" / "paper_fills.jsonl"))
    gross_profit = sum(item for item in closed_pnls if item > 0)
    gross_loss = abs(sum(item for item in closed_pnls if item < 0))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else (math.inf if gross_profit > 0 else 0.0)
    filled = [order for order in orders.values() if order.status == "filled"]
    rejected = [order for order in orders.values() if order.status == "rejected"]
    unfilled = [order for order in orders.values() if order.status in ("open", "submitted_to_paper_broker", "partially_filled", "cancelled", "expired")]
    ending_equity = equities[-1] if equities else account.initial_cash + account.realized_pnl
    net_return_pct = (ending_equity / account.initial_cash - 1) * 100 if account.initial_cash else 0.0
    rule_violations = sum(
        1
        for item in _read_jsonl(root / "logs" / "audit.jsonl")
        if item.get("event_type") == "safety_rule_violation" or item.get("event") == "safety_rule_violation"
    )
    evaluation = config.get("evaluation", {})
    sufficient = len(sessions) >= int(evaluation.get("minimum_forward_sessions", 20)) and len(closed_pnls) >= int(evaluation.get("minimum_closed_trades", 30))
    passed = bool(
        sufficient
        and net_return_pct > float(evaluation.get("minimum_net_return_pct", 0))
        and profit_factor >= float(evaluation.get("minimum_profit_factor", 1.2))
        and _max_drawdown_pct(equities) <= float(evaluation.get("maximum_drawdown_pct", 10))
        and rule_violations <= int(evaluation.get("maximum_rule_violations", 0))
    )
    labels = evaluation.get("profitability_labels", {})
    profitability = labels.get("passed", "profitable_candidate") if passed else (
        labels.get("insufficient", "insufficient_forward_evidence") if not sufficient else labels.get("failed", "not_profitable")
    )
    return {
        "initial_cash": account.initial_cash,
        "cash": round(account.cash, 4),
        "ending_equity": round(ending_equity, 4),
        "net_return_pct": round(net_return_pct, 4),
        "realized_pnl": round(account.realized_pnl, 4),
        "open_position_count": len(positions),
        "order_count": len(orders),
        "filled_order_count": len(filled),
        "rejected_order_count": len(rejected),
        "unfilled_order_count": len(unfilled),
        "fill_rate": round(len(filled) / len(orders), 4) if orders else 0.0,
        "unfilled_rate": round(len(unfilled) / len(orders), 4) if orders else 0.0,
        "closed_trade_count": len(closed_pnls),
        "win_rate": round(sum(item > 0 for item in closed_pnls) / len(closed_pnls), 4) if closed_pnls else 0.0,
        "profit_factor": round(profit_factor, 4) if math.isfinite(profit_factor) else "infinity",
        "max_drawdown_pct": round(_max_drawdown_pct(equities), 4),
        "forward_session_count": len(sessions),
        "rule_violations": rule_violations,
        "evidence_sufficient": sufficient,
        "promotion_eligible": passed,
        "profitability": profitability,
        "evaluation_thresholds": evaluation,
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    args = parser.parse_args()
    print(json.dumps(calculate_metrics(args.root), indent=2, sort_keys=True))
