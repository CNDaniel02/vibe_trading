from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from scripts.core.models import Account, Order, Position, Quote, parse_ts
from scripts.risk.shared_portfolio_risk import check_shared_entry
from scripts.runtime.market_clock import UsEquityMarketClock


_MARKET_CLOCK = UsEquityMarketClock()


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    reason: str


def validate_order_session(now: str, config: dict, *, is_entry: bool) -> RiskDecision:
    clock = _MARKET_CLOCK.status(now)
    if not clock.is_regular:
        return RiskDecision(False, f"outside regular market session: {clock.market_session}")
    if (
        is_entry
        and clock.minutes_to_close is not None
        and clock.minutes_to_close
        <= int(config.get("paper", {}).get("exit_before_close_minutes", 10))
    ):
        return RiskDecision(False, "new entries blocked before market close")
    return RiskDecision(True, "market session ok")


def validate_quote(quote: Quote | None, now: str, max_age_seconds: int, universe: dict) -> RiskDecision:
    if quote is None:
        return RiskDecision(False, "missing quote")
    if quote.bid <= 0 or quote.ask <= 0 or quote.ask < quote.bid:
        return RiskDecision(False, "invalid quote")
    quote_ts = parse_ts(quote.asof)
    now_ts = parse_ts(now)
    if quote_ts > now_ts + timedelta(seconds=1):
        return RiskDecision(False, "future quote would create lookahead")
    if now_ts - quote_ts > timedelta(seconds=max_age_seconds):
        return RiskDecision(False, "stale quote")
    if quote.halted:
        return RiskDecision(False, "halted symbol")
    if quote.is_otc and not universe.get("allow_otc", False):
        return RiskDecision(False, "OTC not allowed")
    if quote.is_leveraged_etf and not universe.get("allow_leveraged_etf", False):
        return RiskDecision(False, "leveraged ETF not allowed")
    if quote.is_inverse_etf and not universe.get("allow_inverse_etf", False):
        return RiskDecision(False, "inverse ETF not allowed")
    if quote.asset_class not in set(universe.get("allowed_asset_classes", [])):
        return RiskDecision(False, "asset class not allowed")
    if quote.avg_daily_volume_usd is not None:
        if quote.avg_daily_volume_usd < float(universe.get("min_avg_daily_volume_usd", 0)):
            return RiskDecision(False, "insufficient liquidity")
    if quote.last < float(universe.get("min_price_usd", 0)):
        return RiskDecision(False, "price below floor")
    if quote.spread_bps() > float(universe.get("max_spread_bps", 999999)):
        return RiskDecision(False, "spread too wide")
    return RiskDecision(True, "quote ok")


def check_order(
    order: Order,
    quote: Quote | None,
    account: Account,
    positions: dict[str, Position],
    open_orders: dict[str, Order],
    counters: dict,
    config: dict,
    now: str,
    option_positions: dict | None = None,
    option_orders: dict | None = None,
) -> RiskDecision:
    risk = config["risk"]
    universe = config["universe"]
    quote_decision = validate_quote(
        quote,
        now,
        int(config["paper"].get("quote_stale_after_seconds", 60)),
        universe,
    )
    if not quote_decision.approved:
        return quote_decision
    assert quote is not None

    if order.side not in ("buy", "sell"):
        return RiskDecision(False, "unsupported side")
    session_decision = validate_order_session(
        now,
        config,
        is_entry=order.side == "buy",
    )
    if not session_decision.approved:
        return session_decision
    if order.side == "sell" and float(positions.get(order.symbol, Position(order.symbol, 0, 0, now, now)).quantity) <= 0:
        return RiskDecision(False, "sell without position")
    if order.side == "buy" and not universe.get("allow_long_only", True):
        return RiskDecision(False, "long entries disabled")
    if order.quantity <= 0:
        return RiskDecision(False, "quantity must be positive")

    if order.side == "buy" and int(counters.get("trades", 0)) >= int(risk.get("max_daily_trades", 0)):
        return RiskDecision(False, "max daily trades reached")

    daily_loss_limit = account.initial_cash * float(risk.get("max_daily_loss_pct_of_initial_equity", 1))
    if order.side == "buy" and float(counters.get("daily_realized_pnl", 0)) <= -daily_loss_limit:
        return RiskDecision(False, "max daily loss reached")

    if risk.get("reject_duplicate_open_order", True):
        for existing in open_orders.values():
            if existing.status in ("created", "submitted_to_paper_broker", "open", "partially_filled"):
                if existing.idempotency_key and existing.idempotency_key == order.idempotency_key:
                    return RiskDecision(False, "duplicate idempotency key")
                if existing.symbol == order.symbol and existing.side == order.side:
                    return RiskDecision(False, "duplicate open order")

    estimated_price = quote.ask if order.side == "buy" else quote.bid
    notional = estimated_price * order.quantity
    equity = account.equity(positions, {quote.symbol: quote})
    if order.side == "buy":
        if notional >= equity:
            return RiskDecision(False, "all-in order blocked")
        if notional > equity * float(risk.get("max_order_pct_of_equity", 1)):
            return RiskDecision(False, "max order size exceeded")

    current_position = positions.get(order.symbol)
    if order.side == "buy":
        current_value = current_position.market_value(quote) if current_position else 0
        if current_value + notional > equity * float(risk.get("max_position_pct_of_equity", 1)):
            return RiskDecision(False, "max position size exceeded")
        if current_position and not risk.get("allow_average_down", False):
            if quote.mid < current_position.average_price:
                return RiskDecision(False, "average down blocked")
        if current_position and not risk.get("allow_add_to_position", False):
            return RiskDecision(False, "adding to an existing position is blocked")
        if len(positions) >= int(risk.get("max_open_positions", 999)) and current_position is None:
            return RiskDecision(False, "max open positions reached")

        shared = check_shared_entry(
            line="equity",
            new_risk_usd=notional,
            account=account,
            equity_positions=positions,
            option_positions=option_positions or {},
            equity_orders=open_orders,
            option_orders=option_orders or {},
            counters=counters,
            shared_config=config.get("shared_risk", {}),
        )
        if not shared.approved:
            return RiskDecision(False, shared.reason)

    if order.side == "sell" and current_position and order.quantity > current_position.quantity + 1e-9:
        return RiskDecision(False, "sell quantity exceeds position")

    return RiskDecision(True, "approved")


def is_regular_session(now: str) -> bool:
    return _MARKET_CLOCK.status(now).is_regular
