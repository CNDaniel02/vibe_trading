from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Iterator

from scripts.core.models import Quote, parse_ts


@dataclass(frozen=True)
class MarketEvent:
    timestamp: str
    symbol: str
    quote: Quote


def stream_events(quotes: Iterable[Quote]) -> Iterator[MarketEvent]:
    ordered = sorted(quotes, key=lambda quote: (parse_ts(quote.asof), quote.symbol))
    for quote in ordered:
        yield MarketEvent(timestamp=quote.asof, symbol=quote.symbol, quote=quote)
