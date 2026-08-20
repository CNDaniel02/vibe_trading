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
    return [pnl for _, pnl in _closed_option_trade_results(fill_records)]


def _closed_option_trade_results(
    fill_records: list[dict[str, Any]],
) -> list[tuple[str, float]]:
    holdings: dict[str, dict[str, Any]] = {}
    results: list[tuple[str, float]] = []
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
        holding = holdings.setdefault(
            option_id,
            {
                "quantity": 0.0,
                "average_price": 0.0,
                "multiplier": float(multiplier),
                "option_type": str(fill.get("option_type", "unknown")),
            },
        )
        if fill.get("intent") == "buy_to_open":
            new_quantity = holding["quantity"] + quantity
            holding["average_price"] = (
                holding["quantity"] * holding["average_price"] + quantity * price + commission / multiplier
            ) / new_quantity
            holding["quantity"] = new_quantity
        elif fill.get("intent") == "sell_to_close" and holding["quantity"] >= quantity:
            pnl = quantity * multiplier * (price - holding["average_price"]) - commission
            results.append((str(holding["option_type"]), pnl))
            holding["quantity"] -= quantity
            if holding["quantity"] == 0:
                holdings.pop(option_id, None)
    return results


def round_trip_cost_decomposition(
    equity_fill_records: list[dict[str, Any]],
    option_fill_records: list[dict[str, Any]],
) -> dict[str, Any]:
    trips = [
        *_cost_trips(
            equity_fill_records,
            identity_key="symbol",
            entry_action=("side", "buy"),
            exit_action=("side", "sell"),
            default_multiplier=1,
        ),
        *_cost_trips(
            option_fill_records,
            identity_key="option_id",
            entry_action=("intent", "buy_to_open"),
            exit_action=("intent", "sell_to_close"),
            default_multiplier=100,
        ),
    ]
    keys = (
        "gross_midpoint_pnl_usd",
        "spread_cost_usd",
        "slippage_and_tick_cost_usd",
        "commission_usd",
        "executable_net_pnl_usd",
        "identity_residual_usd",
    )
    result = {
        key: round(sum(float(trip[key]) for trip in trips), 10)
        for key in keys
    }
    result["closed_round_trip_count"] = len(trips)
    result["round_trips"] = trips
    result["identity_valid"] = abs(result["identity_residual_usd"]) <= 1e-8
    return result


def _cost_trips(
    records: list[dict[str, Any]],
    *,
    identity_key: str,
    entry_action: tuple[str, str],
    exit_action: tuple[str, str],
    default_multiplier: int,
) -> list[dict[str, Any]]:
    lots: dict[str, list[dict[str, Any]]] = {}
    trips: list[dict[str, Any]] = []
    ordered = sorted(
        records,
        key=lambda record: str(record.get("fill", record).get("filled_at", "")),
    )
    for record in ordered:
        fill = record.get("fill", record)
        quote = record.get("quote", {})
        identity = str(fill.get(identity_key, ""))
        quantity = float(fill.get("quantity", 0))
        if not identity or quantity <= 0:
            continue
        action_name, entry_value = entry_action
        exit_name, exit_value = exit_action
        if fill.get(action_name) == entry_value:
            lots.setdefault(identity, []).append(
                {
                    "remaining": quantity,
                    "original_quantity": quantity,
                    "fill": fill,
                    "quote": quote,
                }
            )
            continue
        if fill.get(exit_name) != exit_value:
            continue
        remaining_exit = quantity
        for lot in lots.get(identity, []):
            if remaining_exit <= 1e-12:
                break
            matched = min(float(lot["remaining"]), remaining_exit)
            if matched <= 0:
                continue
            entry_fill = lot["fill"]
            entry_quote = lot["quote"]
            multiplier = float(fill.get("multiplier", entry_fill.get("multiplier", default_multiplier)))
            entry_mid = (float(entry_quote["bid"]) + float(entry_quote["ask"])) / 2
            exit_mid = (float(quote["bid"]) + float(quote["ask"])) / 2
            gross_midpoint = (exit_mid - entry_mid) * matched * multiplier
            spread = (
                float(entry_quote["ask"])
                - entry_mid
                + exit_mid
                - float(quote["bid"])
            ) * matched * multiplier
            slippage = (
                float(entry_fill["price"])
                - float(entry_quote["ask"])
                + float(quote["bid"])
                - float(fill["price"])
            ) * matched * multiplier
            commission = (
                float(entry_fill.get("commission", 0))
                * matched
                / float(lot["original_quantity"])
                + float(fill.get("commission", 0)) * matched / quantity
            )
            executable_net = (
                (float(fill["price"]) - float(entry_fill["price"]))
                * matched
                * multiplier
                - commission
            )
            residual = gross_midpoint - spread - slippage - commission - executable_net
            trips.append(
                {
                    "identity": identity,
                    "quantity": matched,
                    "multiplier": multiplier,
                    "gross_midpoint_pnl_usd": round(gross_midpoint, 10),
                    "spread_cost_usd": round(spread, 10),
                    "slippage_and_tick_cost_usd": round(slippage, 10),
                    "commission_usd": round(commission, 10),
                    "executable_net_pnl_usd": round(executable_net, 10),
                    "identity_residual_usd": round(residual, 10),
                }
            )
            lot["remaining"] = float(lot["remaining"]) - matched
            remaining_exit -= matched
        lots[identity] = [lot for lot in lots.get(identity, []) if float(lot["remaining"]) > 1e-12]
    return trips


def _ai_directional_breakdown(
    root: Path,
    equity_closed_pnls: list[float],
    option_trade_results: list[tuple[str, float]],
    equity_fill_records: list[dict[str, Any]],
    option_fill_records: list[dict[str, Any]],
) -> dict[str, Any]:
    buckets: dict[str, dict[str, Any]] = {
        direction: {
            "decision_count": 0,
            "proposal_count": 0,
            "filled_entry_count": 0,
            "rejection_reasons": {},
        }
        for direction in ("bullish", "bearish")
    }
    for record in _read_jsonl(root / "logs" / "ai_gated_decisions.jsonl"):
        decision = record.get("decision", {})
        execution = record.get("execution", {})
        direction = str(record.get("ranking", {}).get("direction", ""))
        if direction not in buckets:
            direction = "bearish" if decision.get("instrument") == "put" else "bullish"
        bucket = buckets[direction]
        bucket["decision_count"] += 1
        if decision.get("action") in {"buy", "buy_to_open"}:
            bucket["proposal_count"] += 1
        if execution.get("status") == "filled":
            bucket["filled_entry_count"] += 1
        elif execution.get("reason"):
            reason = str(execution["reason"])
            reasons = bucket["rejection_reasons"]
            reasons[reason] = int(reasons.get(reason, 0)) + 1

    directional_pnls = {
        "bullish": [
            *equity_closed_pnls,
            *(pnl for option_type, pnl in option_trade_results if option_type == "call"),
        ],
        "bearish": [
            pnl for option_type, pnl in option_trade_results if option_type == "put"
        ],
    }
    modeled_costs = {"bullish": 0.0, "bearish": 0.0}
    for record in equity_fill_records:
        fill = record.get("fill", record)
        modeled_costs["bullish"] += (
            float(fill.get("slippage_usd_per_share", 0))
            * float(fill.get("quantity", 0))
            + float(fill.get("commission", 0))
        )
    for record in option_fill_records:
        fill = record.get("fill", record)
        direction = "bearish" if fill.get("option_type") == "put" else "bullish"
        modeled_costs[direction] += (
            float(fill.get("slippage_usd_per_contract", 0))
            * int(fill.get("quantity", 0))
            * int(fill.get("multiplier", 100))
            + float(fill.get("commission", 0))
        )

    for direction, bucket in buckets.items():
        pnls = directional_pnls[direction]
        proposals = int(bucket["proposal_count"])
        bucket["fill_rate"] = round(
            int(bucket["filled_entry_count"]) / proposals,
            4,
        ) if proposals else 0.0
        bucket["closed_trade_count"] = len(pnls)
        bucket["win_rate"] = round(
            sum(pnl > 0 for pnl in pnls) / len(pnls),
            4,
        ) if pnls else 0.0
        bucket["net_pnl"] = round(sum(pnls), 4)
        bucket["modeled_cost_usd"] = round(modeled_costs[direction], 4)
        bucket["rejection_reasons"] = dict(
            sorted(bucket["rejection_reasons"].items())
        )
    return buckets


def _line_metrics(orders: list[Any], closed_pnls: list[float], net_pnl: float) -> dict[str, Any]:
    filled = [order for order in orders if order.status == "filled"]
    rejected = [order for order in orders if order.status == "rejected"]
    unfilled = [order for order in orders if order.status in ("open", "submitted_to_paper_broker", "partially_filled", "cancelled", "expired")]
    executable = [*filled, *unfilled]
    gross_profit = sum(item for item in closed_pnls if item > 0)
    gross_loss = abs(sum(item for item in closed_pnls if item < 0))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else (math.inf if gross_profit > 0 else 0.0)
    return {
        "net_pnl": round(net_pnl, 4),
        "order_count": len(orders),
        "filled_order_count": len(filled),
        "rejected_order_count": len(rejected),
        "execution_order_count": len(executable),
        "unfilled_order_count": len(unfilled),
        "fill_rate": round(len(filled) / len(executable), 4) if executable else 0.0,
        "unfilled_rate": round(len(unfilled) / len(executable), 4) if executable else 0.0,
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


def _snapshot_matches_current_state(
    snapshot: dict[str, Any],
    account: Account,
    positions: dict[str, Position],
    option_positions: dict[str, OptionPosition],
) -> bool:
    try:
        if abs(float(snapshot.get("cash")) - float(account.cash)) > 0.001:
            return False
    except (TypeError, ValueError):
        return False
    snapshot_positions = snapshot.get("positions")
    snapshot_option_positions = snapshot.get("option_positions")
    if not isinstance(snapshot_positions, dict) or not isinstance(snapshot_option_positions, dict):
        return False
    if set(snapshot_positions) != set(positions):
        return False
    if set(snapshot_option_positions) != set(option_positions):
        return False
    for symbol, position in positions.items():
        value = snapshot_positions.get(symbol)
        if not isinstance(value, dict):
            return False
        if abs(float(value.get("quantity", -1)) - float(position.quantity)) > 1e-9:
            return False
        if abs(float(value.get("average_price", -1)) - float(position.average_price)) > 0.0001:
            return False
    for option_id, position in option_positions.items():
        value = snapshot_option_positions.get(option_id)
        if not isinstance(value, dict):
            return False
        if int(value.get("quantity", -1)) != int(position.quantity):
            return False
        if abs(float(value.get("average_price", -1)) - float(position.average_price)) > 0.0001:
            return False
    return True


def calculate_metrics(root: str | Path, namespace: str | None = None) -> dict[str, Any]:
    root = Path(root)
    config = load_runtime_config(root)
    state_dir = root / "state"
    log_dir = root / "logs"
    if namespace:
        state_dir = state_dir / "strategy_sleeves" / namespace
        log_dir = log_dir / "strategy_sleeves" / namespace
    account = Account.from_dict(json.loads((state_dir / "paper_account.json").read_text(encoding="utf-8")))
    positions = {
        symbol: Position.from_dict(value)
        for symbol, value in json.loads((state_dir / "paper_positions.json").read_text(encoding="utf-8")).items()
    }
    orders = {
        order_id: Order.from_dict(value)
        for order_id, value in json.loads((state_dir / "paper_orders.json").read_text(encoding="utf-8")).items()
    }
    option_positions_path = state_dir / "paper_option_positions.json"
    option_orders_path = state_dir / "paper_option_orders.json"
    option_positions = {
        option_id: OptionPosition.from_dict(value)
        for option_id, value in (json.loads(option_positions_path.read_text(encoding="utf-8")) if option_positions_path.exists() else {}).items()
    }
    option_orders = {
        order_id: OptionOrder.from_dict(value)
        for order_id, value in (json.loads(option_orders_path.read_text(encoding="utf-8")) if option_orders_path.exists() else {}).items()
    }
    snapshots = _read_jsonl(log_dir / "portfolio_snapshots.jsonl")
    valid_snapshots = [item for item in snapshots if item.get("equity") is not None]
    equities = [float(item["equity"]) for item in valid_snapshots]
    sessions = {str(item.get("session")) for item in valid_snapshots if item.get("session")}
    equity_fill_records = _read_jsonl(log_dir / "paper_fills.jsonl")
    option_fill_records = _read_jsonl(log_dir / "paper_option_fills.jsonl")
    closed_pnls = _closed_trade_pnls(equity_fill_records)
    option_trade_results = _closed_option_trade_results(option_fill_records)
    execution_cost_decomposition = round_trip_cost_decomposition(
        equity_fill_records,
        option_fill_records,
    )
    option_closed_pnls = [pnl for _, pnl in option_trade_results]
    all_closed_pnls = [*closed_pnls, *option_closed_pnls]
    gross_profit = sum(item for item in all_closed_pnls if item > 0)
    gross_loss = abs(sum(item for item in all_closed_pnls if item < 0))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else (math.inf if gross_profit > 0 else 0.0)
    all_orders = [*orders.values(), *option_orders.values()]
    filled = [order for order in all_orders if order.status == "filled"]
    rejected = [order for order in all_orders if order.status == "rejected"]
    unfilled = [order for order in all_orders if order.status in ("open", "submitted_to_paper_broker", "partially_filled", "cancelled", "expired")]
    executable = [*filled, *unfilled]
    current_snapshot = next(
        (
            item
            for item in reversed(valid_snapshots)
            if _snapshot_matches_current_state(item, account, positions, option_positions)
        ),
        None,
    )
    if not positions and not option_positions:
        ending_equity = float(account.cash)
        valuation_status = "cash_flat"
        valuation_asof = account.updated_at
    elif current_snapshot is not None:
        ending_equity = float(current_snapshot["equity"])
        valuation_status = "current_mark_to_market"
        valuation_asof = current_snapshot.get("asof") or current_snapshot.get("ts")
    else:
        ending_equity = (
            float(account.cash)
            + sum(position.average_price * position.quantity for position in positions.values())
            + sum(position.cost_basis() for position in option_positions.values())
        )
        valuation_status = "cost_basis_fallback"
        valuation_asof = account.updated_at
    net_return_pct = (ending_equity / account.initial_cash - 1) * 100 if account.initial_cash else 0.0
    rule_violations = sum(
        1
        for item in _read_jsonl(log_dir / "audit.jsonl")
        if item.get("event_type") == "safety_rule_violation" or item.get("event") == "safety_rule_violation"
    )
    evaluation = config.get("evaluation", {})
    drawdown_equities = [float(account.initial_cash), *equities]
    if not drawdown_equities or abs(drawdown_equities[-1] - ending_equity) > 0.0001:
        drawdown_equities.append(ending_equity)
    maximum_drawdown_pct = _max_drawdown_pct(drawdown_equities)
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
        sum(closed_pnls)
        + (
            float(current_snapshot.get("equity_unrealized_pnl", 0) or 0)
            if current_snapshot is not None
            else 0
        ),
    )
    options_line = _line_metrics(
        list(option_orders.values()),
        option_closed_pnls,
        sum(option_closed_pnls)
        + (
            float(current_snapshot.get("option_unrealized_pnl", 0) or 0)
            if current_snapshot is not None
            else 0
        ),
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
    result = {
        "namespace": namespace,
        "initial_cash": account.initial_cash,
        "cash": round(account.cash, 4),
        "ending_equity": round(ending_equity, 4),
        "valuation_status": valuation_status,
        "valuation_asof": valuation_asof,
        "net_return_pct": round(net_return_pct, 4),
        "realized_pnl": round(account.realized_pnl, 4),
        "open_position_count": len(positions) + len(option_positions),
        "order_count": len(all_orders),
        "filled_order_count": len(filled),
        "rejected_order_count": len(rejected),
        "execution_order_count": len(executable),
        "unfilled_order_count": len(unfilled),
        "fill_rate": round(len(filled) / len(executable), 4) if executable else 0.0,
        "unfilled_rate": round(len(unfilled) / len(executable), 4) if executable else 0.0,
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
        "execution_cost_decomposition": execution_cost_decomposition,
    }
    if namespace == "ai_gated_technical_v1":
        result["directional_breakdown"] = _ai_directional_breakdown(
            root,
            closed_pnls,
            option_trade_results,
            equity_fill_records,
            option_fill_records,
        )
    return result


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    args = parser.parse_args()
    print(json.dumps(calculate_metrics(args.root), indent=2, sort_keys=True))
