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
    """Return a whole-share size below the deterministic order cap."""
    equity = account.equity(positions, {quote.symbol: quote})
    risk_fraction = float(risk_config.get("max_order_pct_of_equity", 0.25))
    budget = equity * risk_fraction * max(0.0, min(1.0, notional_buffer_pct))
    return float(max(0, math.floor(budget / quote.ask))) if quote.ask > 0 else 0.0
