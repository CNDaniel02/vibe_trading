from __future__ import annotations

import csv
from pathlib import Path

from scripts.core.models import Quote
from scripts.replay.event_stream import MarketEvent, stream_events


def _float_or_none(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


class CsvHistoricalMarketDataAdapter:
    """CSV adapter for visible-at-time historical quotes.

    Required columns: timestamp, symbol, bid, ask.
    Optional columns: last, source, avg_daily_volume_usd, asset_class, halted.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load_quotes(self) -> list[Quote]:
        quotes: list[Quote] = []
        with self.path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            required = {"timestamp", "symbol", "bid", "ask"}
            missing = required.difference(reader.fieldnames or [])
            if missing:
                raise ValueError(f"historical quote CSV missing columns: {sorted(missing)}")
            for row in reader:
                bid = float(row["bid"])
                ask = float(row["ask"])
                quotes.append(
                    Quote(
                        symbol=row["symbol"].upper(),
                        bid=bid,
                        ask=ask,
                        last=float(row.get("last") or ((bid + ask) / 2)),
                        asof=row["timestamp"],
                        source=row.get("source") or "historical_csv",
                        avg_daily_volume_usd=_float_or_none(row.get("avg_daily_volume_usd")),
                        asset_class=row.get("asset_class") or "us_equity",
                        halted=(row.get("halted") or "").lower() == "true",
                    )
                )
        return quotes

    def events(self) -> list[MarketEvent]:
        return list(stream_events(self.load_quotes()))
