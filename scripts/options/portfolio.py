from __future__ import annotations

from typing import Any

from scripts.options.models import OptionPosition, OptionQuote


def aggregate_portfolio_greeks(
    positions: dict[str, OptionPosition],
    quotes: dict[str, OptionQuote],
) -> dict[str, Any]:
    totals = {"delta": 0.0, "gamma": 0.0, "theta_per_day": 0.0, "vega_per_vol_point": 0.0}
    missing: list[str] = []
    for option_id, position in positions.items():
        quote = quotes.get(option_id)
        if quote is None or any(value is None for value in (quote.delta, quote.gamma, quote.theta, quote.vega)):
            missing.append(option_id)
            continue
        scale = position.quantity * position.contract.multiplier
        totals["delta"] += float(quote.delta) * scale
        totals["gamma"] += float(quote.gamma) * scale
        totals["theta_per_day"] += float(quote.theta) * scale
        totals["vega_per_vol_point"] += float(quote.vega) * scale
    return {
        **{key: round(value, 6) for key, value in totals.items()},
        "missing_option_ids": sorted(missing),
        "complete": not missing,
    }
