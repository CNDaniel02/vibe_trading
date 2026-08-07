from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from scripts.core.models import Account, Order, Position


@dataclass(frozen=True)
class SharedRiskDecision:
    approved: bool
    reason: str
    account_equity_at_cost: float
    total_deployed_after: float
    line_deployed_after: float


_OPEN_STATUSES = {"created", "submitted_to_paper_broker", "open", "partially_filled"}


def _value(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _equity_position_cost(positions: dict[str, Position]) -> float:
    return sum(float(position.quantity) * float(position.average_price) for position in positions.values())


def _option_position_cost(positions: dict[str, Any]) -> float:
    total = 0.0
    for position in positions.values():
        contract = _value(position, "contract", {})
        multiplier = float(_value(contract, "multiplier", 100))
        total += float(_value(position, "quantity", 0)) * float(_value(position, "average_price", 0)) * multiplier
    return total


def _pending_equity_cost(orders: dict[str, Order]) -> float:
    total = 0.0
    for order in orders.values():
        if order.status in _OPEN_STATUSES and order.side == "buy":
            total += float(order.quantity) * float(order.limit_price or 0)
    return total


def _pending_option_cost(orders: dict[str, Any]) -> float:
    total = 0.0
    for order in orders.values():
        if _value(order, "status") not in _OPEN_STATUSES or _value(order, "intent") != "buy_to_open":
            continue
        contract = _value(order, "contract", {})
        total += (
            float(_value(order, "quantity", 0))
            * float(_value(order, "limit_price", 0) or 0)
            * float(_value(contract, "multiplier", 100))
        )
    return total


def shared_deployment(
    account: Account,
    equity_positions: dict[str, Position],
    option_positions: dict[str, Any],
    equity_orders: dict[str, Order],
    option_orders: dict[str, Any],
    *,
    reserve_open_orders: bool = True,
) -> dict[str, float]:
    equity = _equity_position_cost(equity_positions)
    options = _option_position_cost(option_positions)
    if reserve_open_orders:
        equity += _pending_equity_cost(equity_orders)
        options += _pending_option_cost(option_orders)
    account_equity_at_cost = float(account.cash) + _equity_position_cost(equity_positions) + _option_position_cost(option_positions)
    return {
        "account_equity_at_cost": account_equity_at_cost,
        "equity_deployed": equity,
        "options_deployed": options,
        "total_deployed": equity + options,
    }


def shared_entry_capacity(
    *,
    line: str,
    account: Account,
    equity_positions: dict[str, Position],
    option_positions: dict[str, Any],
    equity_orders: dict[str, Order],
    option_orders: dict[str, Any],
    shared_config: dict[str, Any],
) -> float:
    """Return the remaining cash-backed entry capacity for one strategy line."""
    deployment = shared_deployment(
        account,
        equity_positions,
        option_positions,
        equity_orders,
        option_orders,
        reserve_open_orders=bool(shared_config.get("reserve_open_orders", True)),
    )
    if not shared_config.get("enabled", True):
        return max(0.0, float(account.cash))
    account_equity = deployment["account_equity_at_cost"]
    if account_equity <= 0:
        return 0.0
    line_name = "options_deployed" if line == "options" else "equity_deployed"
    line_cap_name = (
        "max_options_deployed_pct_of_equity"
        if line == "options"
        else "max_equity_deployed_pct_of_equity"
    )
    total_remaining = (
        account_equity * float(shared_config.get("max_total_deployed_pct_of_equity", 1))
        - deployment["total_deployed"]
    )
    line_remaining = (
        account_equity * float(shared_config.get(line_cap_name, 1))
        - deployment[line_name]
    )
    return max(0.0, min(float(account.cash), total_remaining, line_remaining))


def check_shared_entry(
    *,
    line: str,
    new_risk_usd: float,
    account: Account,
    equity_positions: dict[str, Position],
    option_positions: dict[str, Any],
    equity_orders: dict[str, Order],
    option_orders: dict[str, Any],
    counters: dict[str, Any],
    shared_config: dict[str, Any],
) -> SharedRiskDecision:
    deployment = shared_deployment(
        account,
        equity_positions,
        option_positions,
        equity_orders,
        option_orders,
        reserve_open_orders=bool(shared_config.get("reserve_open_orders", True)),
    )
    account_equity = deployment["account_equity_at_cost"]
    line_deployed = deployment["options_deployed" if line == "options" else "equity_deployed"]
    total_after = deployment["total_deployed"] + new_risk_usd
    line_after = line_deployed + new_risk_usd
    decision = SharedRiskDecision(True, "shared account risk approved", account_equity, total_after, line_after)
    if not shared_config.get("enabled", True):
        return decision
    if new_risk_usd <= 0:
        return SharedRiskDecision(False, "entry risk must be positive", account_equity, total_after, line_after)
    if new_risk_usd > account.cash + 1e-9:
        return SharedRiskDecision(False, "insufficient shared paper cash", account_equity, total_after, line_after)
    if int(counters.get("trades", 0)) >= int(shared_config.get("max_total_daily_entry_trades", 0)):
        return SharedRiskDecision(False, "shared max daily trades reached", account_equity, total_after, line_after)
    if account_equity <= 0:
        return SharedRiskDecision(False, "shared account equity is non-positive", account_equity, total_after, line_after)
    if total_after > account_equity * float(shared_config.get("max_total_deployed_pct_of_equity", 1)) + 1e-9:
        return SharedRiskDecision(False, "shared total deployed risk cap exceeded", account_equity, total_after, line_after)
    line_key = "max_options_deployed_pct_of_equity" if line == "options" else "max_equity_deployed_pct_of_equity"
    if line_after > account_equity * float(shared_config.get(line_key, 1)) + 1e-9:
        return SharedRiskDecision(False, f"shared {line} deployed risk cap exceeded", account_equity, total_after, line_after)
    return decision
