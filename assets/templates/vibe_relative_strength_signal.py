from __future__ import annotations

from typing import Dict

import pandas as pd


class SignalEngine:
    """Long-only cross-sectional relative-strength research signal."""

    def generate(self, data_map: Dict[str, pd.DataFrame]) -> Dict[str, pd.Series]:
        closes = pd.DataFrame({symbol: frame["close"] for symbol, frame in data_map.items()}).sort_index()
        momentum = closes.pct_change(20)
        percentile = momentum.rank(axis=1, pct=True)
        signals: Dict[str, pd.Series] = {}
        for symbol, frame in data_map.items():
            close = frame["close"]
            trend = close > close.rolling(20, min_periods=20).mean()
            volume = frame.get("volume", pd.Series(0.0, index=frame.index))
            volume_ok = volume >= volume.rolling(20, min_periods=5).mean() * 0.8
            rank = percentile[symbol].reindex(frame.index)
            signals[symbol] = ((rank >= 0.7) & trend & volume_ok).astype(float).fillna(0.0)
        return signals
