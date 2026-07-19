from __future__ import annotations

from scripts.core.models import Quote, utc_now


def pick_first_valid_candidate(quotes: dict[str, Quote], watchlist: list[str]) -> dict | None:
    for symbol in watchlist:
        quote = quotes.get(symbol)
        if quote is None:
            continue
        return {
            "decision_id": f"decision_{symbol}_{utc_now().replace(':', '').replace('+', 'Z')}",
            "symbol": symbol,
            "side": "buy",
            "order_type": "limit",
            "limit_price": quote.ask,
            "quantity": 1.0,
            "decision_time": utc_now(),
            "quote_seen_at": quote.asof,
            "thesis": "First-version paper cycle candidate selected from configured liquid watchlist.",
        }
    return None
