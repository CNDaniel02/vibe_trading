from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from scripts.options.models import OptionFill, OptionOrder, OptionQuote


@dataclass(frozen=True)
class OptionFillDecision:
    status: str
    fill: OptionFill | None = None
    reason: str | None = None


def _round_up_to_tick(value: float, tick: float) -> float:
    if tick <= 0:
        return round(value, 4)
    units = (Decimal(str(value)) / Decimal(str(tick))).quantize(Decimal("1"), rounding=ROUND_CEILING)
    return float(units * Decimal(str(tick)))


def _round_down_to_tick(value: float, tick: float) -> float:
    if tick <= 0:
        return round(value, 4)
    units = (Decimal(str(value)) / Decimal(str(tick))).quantize(Decimal("1"), rounding=ROUND_FLOOR)
    return float(units * Decimal(str(tick)))


def simulate_option_fill(order: OptionOrder, quote: OptionQuote, costs: dict, filled_at: str) -> OptionFillDecision:
    if order.quantity <= 0 or int(order.quantity) != order.quantity:
        return OptionFillDecision("rejected", reason="option quantity must be a positive whole contract count")
    if quote.bid < 0 or quote.ask <= 0 or quote.ask < quote.bid:
        return OptionFillDecision("rejected", reason="invalid option quote")

    bps = float(costs.get("slippage_bps", 0))
    minimum = float(costs.get("minimum_slippage_usd_per_contract", 0))
    reference = quote.ask if order.intent == "buy_to_open" else quote.bid
    slip = max(reference * bps / 10000, minimum)

    if order.intent == "buy_to_open":
        tick_reference = quote.ask + slip
        tick = (
            order.contract.above_tick
            if tick_reference > order.contract.tick_cutoff_price
            else order.contract.below_tick
        )
        tick = tick or float(costs.get("price_tick_usd", 0.01))
        adverse_price = _round_up_to_tick(tick_reference, tick)
        if order.order_type == "limit":
            if order.limit_price is None:
                return OptionFillDecision("rejected", reason="limit buy missing limit_price")
            if adverse_price > order.limit_price:
                return OptionFillDecision("open", reason="adverse option buy fill would exceed limit")
        price = adverse_price
    elif order.intent == "sell_to_close":
        raw_price = max(0.0, quote.bid - slip)
        tick = (
            order.contract.above_tick
            if raw_price > order.contract.tick_cutoff_price
            else order.contract.below_tick
        )
        tick = tick or float(costs.get("price_tick_usd", 0.01))
        adverse_price = max(0.0, _round_down_to_tick(raw_price, tick))
        if order.order_type == "limit":
            if order.limit_price is None:
                return OptionFillDecision("rejected", reason="limit sell missing limit_price")
            if adverse_price < order.limit_price:
                return OptionFillDecision("open", reason="adverse option sell fill would fall below limit")
        price = adverse_price
    else:
        return OptionFillDecision("rejected", reason="unsupported option intent")

    multiplier = order.contract.multiplier
    commission = float(costs.get("commission_per_contract_usd", 0)) * order.quantity
    commission += float(costs.get("regulatory_fee_per_order_usd", 0))
    fill = OptionFill(
        fill_id=f"fill_{order.order_id}",
        order_id=order.order_id,
        option_id=order.contract.option_id,
        underlying=order.contract.underlying,
        option_type=order.contract.option_type,
        intent=order.intent,
        quantity=int(order.quantity),
        price=round(price, 4),
        multiplier=multiplier,
        gross_amount=round(price * order.quantity * multiplier, 4),
        commission=round(commission, 4),
        slippage_usd_per_contract=round(slip, 4),
        quote_asof=quote.updated_at,
        filled_at=filled_at,
    )
    return OptionFillDecision("filled", fill=fill)
