from __future__ import annotations

import hashlib
from typing import Any

from scripts.adapters.vibe_market_data_adapter import MarketBar
from scripts.core.models import Position, Quote, parse_ts
from scripts.runtime.market_clock import MarketClockState


def build_snapshot(
    ticker: str,
    quote: Quote,
    bars: dict[str, list[MarketBar]],
    clock: MarketClockState,
    *,
    available_news: list[dict[str, Any]] | None = None,
    source_metadata: list[dict[str, Any]] | None = None,
    positions: dict[str, Position] | None = None,
) -> dict[str, Any]:
    ticker = ticker.upper()
    decision_time = clock.asof
    cutoff = min(parse_ts(quote.asof), parse_ts(decision_time)).isoformat()
    symbol_bars = _completed_bars(bars.get(ticker, []), cutoff)
    benchmark_bars = _completed_bars(bars.get("SPY", []), cutoff)
    price_1d = _pct_change(quote.last, quote.previous_close or _last_close(symbol_bars))
    price_5d = _pct_change(quote.last, _nth_last_close(symbol_bars, 5))
    symbol_20d = _series_return(symbol_bars, 20)
    benchmark_20d = _series_return(benchmark_bars, 20)
    relative_strength = symbol_20d - benchmark_20d
    average_volume = _average_volume(symbol_bars, 20)
    if quote.session_volume is not None and average_volume:
        expected_volume = average_volume * max(0.1, clock.session_progress)
        volume_ratio = quote.session_volume / expected_volume if expected_volume > 0 else 0.0
    else:
        volume_ratio = 1.0
    chase_score = max(0.0, min(1.0, max(price_1d / 8.0, price_5d / 15.0)))
    benchmark_change = _pct_change(quote.last, quote.previous_close) if ticker == "SPY" else _benchmark_change(bars, cutoff)
    snapshot_key = f"{ticker}|{clock.asof[:16]}|{quote.asof}|{quote.bid:.4f}|{quote.ask:.4f}"
    snapshot_id = f"fwd_{hashlib.sha256(snapshot_key.encode('utf-8')).hexdigest()[:20]}"
    metadata = [
        {"source": quote.source, "kind": "top_of_book", "asof": quote.asof},
        {"source": symbol_bars[-1].source if symbol_bars else "missing", "kind": "completed_ohlcv", "asof": symbol_bars[-1].timestamp if symbol_bars else None},
    ]
    metadata.extend(source_metadata or [])
    return {
        "snapshot_id": snapshot_id,
        "decision_time": decision_time,
        "data_cutoff_time": cutoff,
        "ticker": ticker,
        "market_session": clock.market_session,
        "market_data": {
            "quote": quote.to_dict(),
            "market_regime": "risk_off" if benchmark_change <= -1.5 else ("risk_on" if benchmark_change >= 0.5 else "neutral"),
            "benchmark_change_1d_pct": round(benchmark_change, 4),
            "volatility_change_pct": 0.0,
            "binary_event_within_days": 99,
            "has_position": bool((positions or {}).get(ticker)),
            "shadow_account": {"initial_cash": 2000, "cash": 2000},
        },
        "technical_signals": {
            "relative_strength_20d": round(relative_strength, 4),
            "price_change_1d_pct": round(price_1d, 4),
            "price_change_5d_pct": round(price_5d, 4),
            "volume_ratio": round(volume_ratio, 4),
            "chase_score": round(chase_score, 4),
        },
        "available_news": list(available_news or []),
        "source_metadata": metadata,
    }


def _completed_bars(bars: list[MarketBar], cutoff: str) -> list[MarketBar]:
    cutoff_ts = parse_ts(cutoff)
    return [bar for bar in bars if parse_ts(bar.timestamp).date() < cutoff_ts.date()]


def _last_close(bars: list[MarketBar]) -> float | None:
    return bars[-1].close if bars else None


def _nth_last_close(bars: list[MarketBar], periods: int) -> float | None:
    return bars[-periods].close if len(bars) >= periods else None


def _pct_change(current: float | None, previous: float | None) -> float:
    if current is None or previous is None or previous <= 0:
        return 0.0
    return (current / previous - 1) * 100


def _series_return(bars: list[MarketBar], periods: int) -> float:
    if len(bars) < periods + 1:
        return 0.0
    return _pct_change(bars[-1].close, bars[-periods - 1].close)


def _average_volume(bars: list[MarketBar], periods: int) -> float | None:
    sample = bars[-periods:]
    return sum(bar.volume for bar in sample) / len(sample) if sample else None


def _benchmark_change(bars: dict[str, list[MarketBar]], cutoff: str) -> float:
    completed = _completed_bars(bars.get("SPY", []), cutoff)
    if len(completed) < 2:
        return 0.0
    return _pct_change(completed[-1].close, completed[-2].close)
