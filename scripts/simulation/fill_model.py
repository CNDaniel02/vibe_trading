from __future__ import annotations

from dataclasses import dataclass

from scripts.core.models import Fill, Order, Quote


@dataclass(frozen=True)
class FillDecision:
    status: str
    fill: Fill | None = None
    reason: str | None = None


def adverse_slippage(price: float, slippage_bps: float, minimum_slippage_usd: float) -> float:
    return max(price * slippage_bps / 10000, minimum_slippage_usd)


def simulate_fill(order: Order, quote: Quote, costs: dict, filled_at: str | None = None) -> FillDecision:
    if order.quantity <= 0:
        return FillDecision(status="rejected", reason="quantity must be positive")
    if quote.bid <= 0 or quote.ask <= 0 or quote.ask < quote.bid:
        return FillDecision(status="rejected", reason="invalid quote")

    slip = adverse_slippage(
        quote.ask if order.side == "buy" else quote.bid,
        float(costs.get("slippage_bps", 0)),
        float(costs.get("minimum_slippage_usd", 0)),
    )
    commission = float(costs.get("commission_per_order_usd", 0))

    if order.side == "buy":
        if order.order_type == "limit":
            if order.limit_price is None:
                return FillDecision(status="rejected", reason="limit buy missing limit_price")
            adverse_price = round(quote.ask + slip, 4)
            if adverse_price > order.limit_price:
                return FillDecision(status="open", reason="adverse buy fill would exceed limit")
            price = adverse_price
        else:
            price = round(quote.ask + slip, 4)
    elif order.side == "sell":
        if order.order_type == "limit":
            if order.limit_price is None:
                return FillDecision(status="rejected", reason="limit sell missing limit_price")
            adverse_price = round(max(0.0001, quote.bid - slip), 4)
            if adverse_price < order.limit_price:
                return FillDecision(status="open", reason="adverse sell fill would fall below limit")
            price = adverse_price
        else:
            price = round(max(0.0001, quote.bid - slip), 4)
    else:
        return FillDecision(status="rejected", reason="unsupported side")

    fill = Fill(
        fill_id=f"fill_{order.order_id}",
        order_id=order.order_id,
        symbol=order.symbol,
        side=order.side,
        quantity=order.quantity,
        price=price,
        gross_amount=round(price * order.quantity, 4),
        commission=commission,
        slippage_usd_per_share=round(slip, 4),
        quote_asof=quote.asof,
        filled_at=filled_at or order.updated_at or order.submitted_at or order.created_at,
    )
    return FillDecision(status="filled", fill=fill)
