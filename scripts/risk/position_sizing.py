from __future__ import annotations

import math

from scripts.core.models import Account, Position, Quote


def calculate_entry_quantity(
    account: Account,
    positions: dict[str, Position],
    quote: Quote,
    risk_config: dict,
    *,
    notional_buffer_pct: float = 0.96,
) -> float:
    """Return a deterministic whole or fractional share size below the order cap."""
    equity = account.equity(positions, {quote.symbol: quote})
    risk_fraction = float(risk_config.get("max_order_pct_of_equity", 0.25))
    budget = equity * risk_fraction * max(0.0, min(1.0, notional_buffer_pct))
    if quote.ask <= 0:
        return 0.0
    raw_quantity = budget / quote.ask
    if not bool(risk_config.get("allow_fractional_shares", False)):
        return float(max(0, math.floor(raw_quantity)))
    increment = float(risk_config.get("fractional_share_increment", 0.001))
    if increment <= 0:
        raise ValueError("fractional_share_increment must be positive")
    # Round down to keep the notional safely below the configured risk cap.
    quantity = math.floor((raw_quantity + 1e-12) / increment) * increment
    decimals = max(0, len(f"{increment:.12f}".rstrip("0").split(".")[-1]))
    return round(max(0.0, quantity), decimals)
