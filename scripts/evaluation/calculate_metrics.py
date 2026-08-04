from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from scripts.core.config import load_runtime_config
from scripts.core.models import Account, Order, Position
from scripts.options.models import OptionOrder, OptionPosition


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


def _closed_option_trade_pnls(fill_records: list[dict[str, Any]]) -> list[float]:
    holdings: dict[str, dict[str, float]] = {}
    pnls: list[float] = []
    fills = [record.get("fill", record) for record in fill_records]
    fills.sort(key=lambda fill: str(fill.get("filled_at", "")))
    for fill in fills:
        option_id = str(fill.get("option_id", ""))
        quantity = int(fill.get("quantity", 0))
        price = float(fill.get("price", 0))
        multiplier = int(fill.get("multiplier", 100))
        commission = float(fill.get("commission", 0))
        if not option_id or quantity <= 0 or price < 0:
            continue
        holding = holdings.setdefault(option_id, {"quantity": 0.0, "average_price": 0.0, "multiplier": float(multiplier)})
        if fill.get("intent") == "buy_to_open":
            new_quantity = holding["quantity"] + quantity
            holding["average_price"] = (
                holding["quantity"] * holding["average_price"] + quantity * price + commission / multiplier
            ) / new_quantity
            holding["quantity"] = new_quantity
        elif fill.get("intent") == "sell_to_close" and holding["quantity"] >= quantity:
            pnl = quantity * multiplier * (price - holding["average_price"]) - commission
            pnls.append(pnl)
            holding["quantity"] -= quantity
            if holding["quantity"] == 0:
                holdings.pop(option_id, None)
    return pnls


def _line_metrics(orders: list[Any], closed_pnls: list[float], net_pnl: float) -> dict[str, Any]:
    filled = [order for order in orders if order.status == "filled"]
    rejected = [order for order in orders if order.status == "rejected"]
    unfilled = [order for order in orders if order.status in ("open", "submitted_to_paper_broker", "partially_filled", "cancelled", "expired")]
    gross_profit = sum(item for item in closed_pnls if item > 0)
    gross_loss = abs(sum(item for item in closed_pnls if item < 0))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else (math.inf if gross_profit > 0 else 0.0)
    return {
        "net_pnl": round(net_pnl, 4),
        "order_count": len(orders),
        "filled_order_count": len(filled),
        "rejected_order_count": len(rejected),
        "unfilled_order_count": len(unfilled),
        "fill_rate": round(len(filled) / len(orders), 4) if orders else 0.0,
        "unfilled_rate": round(len(unfilled) / len(orders), 4) if orders else 0.0,
        "closed_trade_count": len(closed_pnls),
        "win_rate": round(sum(item > 0 for item in closed_pnls) / len(closed_pnls), 4) if closed_pnls else 0.0,
        "profit_factor": round(profit_factor, 4) if math.isfinite(profit_factor) else "infinity",
    }


def _classify_line(
    metrics: dict[str, Any],
    *,
    forward_sessions: int,
    initial_cash: float,
    shared_drawdown_pct: float,
    rule_violations: int,
    evaluation: dict[str, Any],
) -> dict[str, Any]:
    result = dict(metrics)
    result["net_return_pct_of_initial_equity"] = round(
        float(result["net_pnl"]) / initial_cash * 100 if initial_cash else 0.0,
        4,
    )
    sufficient = (
        forward_sessions >= int(evaluation.get("minimum_forward_sessions", 20))
        and int(result["closed_trade_count"]) >= int(evaluation.get("minimum_closed_trades", 30))
    )
    raw_profit_factor = result["profit_factor"]
    profit_factor = math.inf if raw_profit_factor == "infinity" else float(raw_profit_factor)
    passed = bool(
        sufficient
        and result["net_return_pct_of_initial_equity"] > float(evaluation.get("minimum_net_return_pct", 0))
        and profit_factor >= float(evaluation.get("minimum_profit_factor", 1.2))
        and shared_drawdown_pct <= float(evaluation.get("maximum_drawdown_pct", 10))
        and rule_violations <= int(evaluation.get("maximum_rule_violations", 0))
    )
    labels = evaluation.get("profitability_labels", {})
    result["evidence_sufficient"] = sufficient
    result["promotion_eligible"] = passed
    result["profitability"] = labels.get("passed", "profitable_candidate") if passed else (
        labels.get("insufficient", "insufficient_forward_evidence")
        if not sufficient
        else labels.get("failed", "not_profitable")
    )
    return result


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
    option_positions_path = root / "state" / "paper_option_positions.json"
    option_orders_path = root / "state" / "paper_option_orders.json"
    option_positions = {
        option_id: OptionPosition.from_dict(value)
        for option_id, value in (json.loads(option_positions_path.read_text(encoding="utf-8")) if option_positions_path.exists() else {}).items()
    }
    option_orders = {
        order_id: OptionOrder.from_dict(value)
        for order_id, value in (json.loads(option_orders_path.read_text(encoding="utf-8")) if option_orders_path.exists() else {}).items()
    }
    snapshots = _read_jsonl(root / "logs" / "portfolio_snapshots.jsonl")
    valid_snapshots = [item for item in snapshots if item.get("equity") is not None]
    equities = [float(item["equity"]) for item in valid_snapshots]
    sessions = {str(item.get("session")) for item in valid_snapshots if item.get("session")}
    closed_pnls = _closed_trade_pnls(_read_jsonl(root / "logs" / "paper_fills.jsonl"))
    option_closed_pnls = _closed_option_trade_pnls(_read_jsonl(root / "logs" / "paper_option_fills.jsonl"))
    all_closed_pnls = [*closed_pnls, *option_closed_pnls]
    gross_profit = sum(item for item in all_closed_pnls if item > 0)
    gross_loss = abs(sum(item for item in all_closed_pnls if item < 0))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else (math.inf if gross_profit > 0 else 0.0)
    all_orders = [*orders.values(), *option_orders.values()]
    filled = [order for order in all_orders if order.status == "filled"]
    rejected = [order for order in all_orders if order.status == "rejected"]
    unfilled = [order for order in all_orders if order.status in ("open", "submitted_to_paper_broker", "partially_filled", "cancelled", "expired")]
    ending_equity = equities[-1] if equities else account.initial_cash + account.realized_pnl
    net_return_pct = (ending_equity / account.initial_cash - 1) * 100 if account.initial_cash else 0.0
    rule_violations = sum(
        1
        for item in _read_jsonl(root / "logs" / "audit.jsonl")
        if item.get("event_type") == "safety_rule_violation" or item.get("event") == "safety_rule_violation"
    )
    evaluation = config.get("evaluation", {})
    maximum_drawdown_pct = _max_drawdown_pct(equities)
    sufficient = len(sessions) >= int(evaluation.get("minimum_forward_sessions", 20)) and len(all_closed_pnls) >= int(evaluation.get("minimum_closed_trades", 30))
    passed = bool(
        sufficient
        and net_return_pct > float(evaluation.get("minimum_net_return_pct", 0))
        and profit_factor >= float(evaluation.get("minimum_profit_factor", 1.2))
        and maximum_drawdown_pct <= float(evaluation.get("maximum_drawdown_pct", 10))
        and rule_violations <= int(evaluation.get("maximum_rule_violations", 0))
    )
    labels = evaluation.get("profitability_labels", {})
    profitability = labels.get("passed", "profitable_candidate") if passed else (
        labels.get("insufficient", "insufficient_forward_evidence") if not sufficient else labels.get("failed", "not_profitable")
    )
    equity_line = _line_metrics(
        list(orders.values()),
        closed_pnls,
        sum(closed_pnls) + float(valid_snapshots[-1].get("equity_unrealized_pnl", 0) or 0) if valid_snapshots else sum(closed_pnls),
    )
    options_line = _line_metrics(
        list(option_orders.values()),
        option_closed_pnls,
        sum(option_closed_pnls) + float(valid_snapshots[-1].get("option_unrealized_pnl", 0) or 0) if valid_snapshots else sum(option_closed_pnls),
    )
    classified_lines = {
        name: _classify_line(
            line,
            forward_sessions=len(sessions),
            initial_cash=account.initial_cash,
            shared_drawdown_pct=maximum_drawdown_pct,
            rule_violations=rule_violations,
            evaluation=evaluation,
        )
        for name, line in {"equity": equity_line, "options": options_line}.items()
    }
    return {
        "initial_cash": account.initial_cash,
        "cash": round(account.cash, 4),
        "ending_equity": round(ending_equity, 4),
        "net_return_pct": round(net_return_pct, 4),
        "realized_pnl": round(account.realized_pnl, 4),
        "open_position_count": len(positions) + len(option_positions),
        "order_count": len(all_orders),
        "filled_order_count": len(filled),
        "rejected_order_count": len(rejected),
        "unfilled_order_count": len(unfilled),
        "fill_rate": round(len(filled) / len(orders), 4) if orders else 0.0,
        "unfilled_rate": round(len(unfilled) / len(orders), 4) if orders else 0.0,
        "closed_trade_count": len(all_closed_pnls),
        "win_rate": round(sum(item > 0 for item in all_closed_pnls) / len(all_closed_pnls), 4) if all_closed_pnls else 0.0,
        "profit_factor": round(profit_factor, 4) if math.isfinite(profit_factor) else "infinity",
        "max_drawdown_pct": round(maximum_drawdown_pct, 4),
        "forward_session_count": len(sessions),
        "rule_violations": rule_violations,
        "evidence_sufficient": sufficient,
        "promotion_eligible": passed,
        "profitability": profitability,
        "evaluation_thresholds": evaluation,
        "lines": classified_lines,
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    args = parser.parse_args()
    print(json.dumps(calculate_metrics(args.root), indent=2, sort_keys=True))
