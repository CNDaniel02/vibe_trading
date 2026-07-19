from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from scripts.adapters.errors import AdapterDataError
from scripts.adapters.vibe_runtime import VibeRuntime
from scripts.core.models import parse_ts


@dataclass(frozen=True)
class MarketBar:
    symbol: str
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    source: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class VibeMarketDataAdapter:
    """Read-only Vibe OHLCV adapter for research and historical replay."""

    def __init__(self, project_root: str | Path, vibe_config: dict[str, Any]) -> None:
        self.project_root = Path(project_root).resolve()
        self.config = vibe_config
        self.runtime = VibeRuntime(self.project_root, vibe_config)
        self.data_config = vibe_config.get("historical_data", {})

    @staticmethod
    def project_symbol(symbol: str) -> str:
        upper = symbol.strip().upper()
        return upper if "." in upper else f"{upper}.US"

    @staticmethod
    def plain_symbol(project_symbol: str) -> str:
        upper = project_symbol.strip().upper()
        return upper[:-3] if upper.endswith(".US") else upper

    def fetch_bars(
        self,
        symbols: list[str],
        start_date: str,
        end_date: str,
        *,
        interval: str | None = None,
        source: str | None = None,
    ) -> dict[str, list[MarketBar]]:
        if not self.data_config.get("enabled", False):
            raise AdapterDataError("Vibe historical data adapter is disabled")
        codes = [self.project_symbol(symbol) for symbol in symbols]
        result = self.runtime.bridge(
            "fetch_bars",
            {
                "codes": codes,
                "start_date": start_date,
                "end_date": end_date,
                "interval": interval or self.data_config.get("interval", "1D"),
                "source": source or self.data_config.get("source", "yahoo"),
            },
        )
        effective_source = str(result.get("source_effective", "unknown"))
        output: dict[str, list[MarketBar]] = {}
        for code, rows in result.get("bars", {}).items():
            symbol = self.plain_symbol(str(code))
            parsed: list[MarketBar] = []
            for row in rows:
                timestamp = str(row["timestamp"])
                values = [float(row[key]) for key in ("open", "high", "low", "close")]
                if min(values) <= 0 or values[1] < values[2]:
                    raise AdapterDataError(f"invalid OHLC bar for {symbol} at {timestamp}")
                parsed.append(
                    MarketBar(
                        symbol=symbol,
                        timestamp=timestamp,
                        open=values[0],
                        high=values[1],
                        low=values[2],
                        close=values[3],
                        volume=float(row.get("volume", 0)),
                        source=f"vibe:{effective_source}",
                    )
                )
            output[symbol] = sorted(parsed, key=lambda item: parse_ts(item.timestamp))
        missing = sorted(set(symbol.upper() for symbol in symbols) - set(output))
        if missing:
            raise AdapterDataError(f"Vibe returned no bars for: {', '.join(missing)}")
        return output

    def fetch_lookback(self, symbols: list[str], decision_time: str) -> dict[str, list[MarketBar]]:
        end = parse_ts(decision_time)
        days = int(self.data_config.get("lookback_calendar_days", 90))
        start = end - timedelta(days=days)
        return self.fetch_bars(symbols, start.date().isoformat(), end.date().isoformat())

    def average_daily_volume_usd(self, bars: list[MarketBar], cutoff_time: str, window: int = 20) -> float | None:
        cutoff = parse_ts(cutoff_time)
        completed = [bar for bar in bars if self._bar_is_completed(bar, cutoff)]
        sample = completed[-window:]
        if not sample:
            return None
        return sum(bar.close * bar.volume for bar in sample) / len(sample)

    @staticmethod
    def _bar_is_completed(bar: MarketBar, cutoff: datetime) -> bool:
        bar_time = parse_ts(bar.timestamp)
        # Daily Vibe bars are date-indexed; today's bar is potentially partial.
        return bar_time.date() < cutoff.astimezone(timezone.utc).date()
