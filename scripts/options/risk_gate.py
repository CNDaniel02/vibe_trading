from __future__ import annotations

from datetime import timedelta

from scripts.core.models import Account, Order, Position, parse_ts
from scripts.options.fill_model import simulate_option_fill
from scripts.options.models import OptionOrder, OptionPosition, OptionQuote
from scripts.risk.risk_gate import RiskDecision, validate_order_session
from scripts.risk.shared_portfolio_risk import check_shared_entry


_OPEN_STATUSES = {"created", "submitted_to_paper_broker", "open", "partially_filled"}


def validate_option_quote(quote: OptionQuote | None, now: str, config: dict, *, enforce_entry_liquidity: bool = True) -> RiskDecision:
    if quote is None:
        return RiskDecision(False, "missing option quote")
    if quote.bid < 0 or quote.ask <= 0 or quote.ask < quote.bid:
        return RiskDecision(False, "invalid option quote")
    quote_time = parse_ts(quote.updated_at)
    now_time = parse_ts(now)
    if quote_time > now_time + timedelta(seconds=1):
        return RiskDecision(False, "future option quote would create lookahead")
    stale_after = int(config.get("options_costs", {}).get("quote_stale_after_seconds", 30))
    if now_time - quote_time > timedelta(seconds=stale_after):
        return RiskDecision(False, "stale option quote")
    if enforce_entry_liquidity:
        universe = config.get("options_universe", {})
        if quote.spread_pct() > float(universe.get("max_spread_pct", 1)):
            return RiskDecision(False, "option spread too wide")
        if int(quote.volume or 0) < int(universe.get("min_volume", 0)):
            return RiskDecision(False, "insufficient option volume")
        if int(quote.open_interest or 0) < int(universe.get("min_open_interest", 0)):
            return RiskDecision(False, "insufficient option open interest")
        if universe.get("require_greeks", True) and any(value is None for value in (quote.delta, quote.gamma, quote.theta, quote.vega)):
            return RiskDecision(False, "missing required option greeks")
        if universe.get("require_implied_volatility", True) and quote.implied_volatility is None:
            return RiskDecision(False, "missing option implied volatility")
    return RiskDecision(True, "option quote ok")


def check_option_order(
    order: OptionOrder,
    quote: OptionQuote | None,
    account: Account,
    equity_positions: dict[str, Position],
    option_positions: dict[str, OptionPosition],
    equity_orders: dict[str, Order],
    option_orders: dict[str, OptionOrder],
    counters: dict,
    config: dict,
    now: str,
) -> RiskDecision:
    if not config.get("risk", {}).get("allow_options", False):
        return RiskDecision(False, "options disabled by equity account mandate")
    risk = config.get("options_risk", {})
    universe = config.get("options_universe", {})
    if not risk.get("enabled", False) or not universe.get("enabled", False):
        return RiskDecision(False, "options paper line is disabled")
    quote_check = validate_option_quote(quote, now, config, enforce_entry_liquidity=order.intent == "buy_to_open")
    if not quote_check.approved:
        return quote_check
    assert quote is not None

    contract = order.contract
    if order.intent not in {"buy_to_open", "sell_to_close"}:
        return RiskDecision(False, "unsupported option intent")
    session_decision = validate_order_session(
        now,
        config,
        is_entry=order.intent == "buy_to_open",
    )
    if not session_decision.approved:
        return session_decision
    if order.quantity <= 0 or int(order.quantity) != order.quantity:
        return RiskDecision(False, "option quantity must be a positive whole number")
    if order.quantity > int(risk.get("max_contracts_per_order", 1)):
        return RiskDecision(False, "max option contracts per order exceeded")
    if contract.option_type not in set(universe.get("allowed_option_types", [])):
        return RiskDecision(False, "option type not allowed")
    if contract.underlying in set(universe.get("excluded_underlyings", [])):
        return RiskDecision(False, "underlying excluded from options universe")
    if contract.multiplier != int(config.get("options_costs", {}).get("contract_multiplier", 100)):
        return RiskDecision(False, "unexpected option contract multiplier")
    dte = contract.dte(now)
    if dte <= 0 and not universe.get("allow_0dte", False):
        return RiskDecision(False, "0DTE and expired contracts are blocked")
    if order.intent == "buy_to_open" and not int(universe.get("min_dte", 1)) <= dte <= int(universe.get("max_dte", 365)):
        return RiskDecision(False, "option DTE outside allowed entry range")

    existing = option_positions.get(contract.option_id)
    if order.intent == "sell_to_close":
        if existing is None or existing.quantity < order.quantity:
            return RiskDecision(False, "sell-to-close exceeds the long option position")
        return RiskDecision(True, "option close approved")

    if not risk.get("long_premium_only", True) or risk.get("allow_sell_to_open", False) or risk.get("allow_short_options", False):
        return RiskDecision(False, "unsafe options mandate configuration")
    if existing is not None:
        return RiskDecision(False, "adding to an existing option position is blocked")
    if len(option_positions) >= int(risk.get("max_open_positions", 1)):
        return RiskDecision(False, "max open option positions reached")
    if int(counters.get("option_trades", 0)) >= int(risk.get("max_daily_entry_trades", 0)):
        return RiskDecision(False, "max daily option trades reached")
    for current in option_orders.values():
        if current.status not in _OPEN_STATUSES:
            continue
        if current.idempotency_key and current.idempotency_key == order.idempotency_key:
            return RiskDecision(False, "duplicate option idempotency key")
        if current.contract.option_id == contract.option_id and current.intent == order.intent:
            return RiskDecision(False, "duplicate open option order")

    delta = abs(float(quote.delta or 0))
    if not float(universe.get("min_abs_delta", 0)) <= delta <= float(universe.get("max_abs_delta", 1)):
        return RiskDecision(False, "option delta outside allowed range")
    simulated = simulate_option_fill(order, quote, config.get("options_costs", {}), now)
    if simulated.status == "rejected":
        return RiskDecision(False, simulated.reason or "option fill model rejected order")
    reference_price = simulated.fill.price if simulated.fill else max(order.limit_price or 0, quote.ask)
    premium_risk = reference_price * order.quantity * contract.multiplier
    equity_at_cost = account.cash
    equity_at_cost += sum(position.average_price * position.quantity for position in equity_positions.values())
    equity_at_cost += sum(position.cost_basis() for position in option_positions.values())
    if premium_risk >= equity_at_cost:
        return RiskDecision(False, "all-in option order blocked")
    if premium_risk > equity_at_cost * float(risk.get("max_order_risk_pct_of_equity", 1)) + 1e-9:
        return RiskDecision(False, "max option premium risk exceeded")

    shared = check_shared_entry(
        line="options",
        new_risk_usd=premium_risk,
        account=account,
        equity_positions=equity_positions,
        option_positions=option_positions,
        equity_orders=equity_orders,
        option_orders=option_orders,
        counters=counters,
        shared_config=config.get("shared_risk", {}),
    )
    if not shared.approved:
        return RiskDecision(False, shared.reason)
    return RiskDecision(True, "option order approved")
