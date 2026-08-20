from __future__ import annotations

from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Any

from scripts.options.models import OptionContract


def option_price_tick(
    contract: OptionContract,
    price: float,
    costs: dict[str, Any],
) -> float:
    tick = (
        contract.above_tick
        if price > contract.tick_cutoff_price
        else contract.below_tick
    )
    return float(tick or costs.get("price_tick_usd", 0.01))


def round_up_to_tick(value: float, tick: float) -> float:
    if tick <= 0:
        return round(value, 4)
    units = (Decimal(str(value)) / Decimal(str(tick))).quantize(
        Decimal("1"),
        rounding=ROUND_CEILING,
    )
    return float(units * Decimal(str(tick)))


def round_down_to_tick(value: float, tick: float) -> float:
    if tick <= 0:
        return round(value, 4)
    units = (Decimal(str(value)) / Decimal(str(tick))).quantize(
        Decimal("1"),
        rounding=ROUND_FLOOR,
    )
    return float(units * Decimal(str(tick)))
