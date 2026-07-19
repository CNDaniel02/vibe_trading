from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from scripts.adapters.errors import AdapterConfigurationError, AdapterDataError
from scripts.adapters.http_json import request_json
from scripts.core.models import Quote


class AlpacaMarketDataAdapter:
    """Read-only Alpaca snapshots adapter. This class has no order methods."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.base_url = str(config.get("base_url", "https://data.alpaca.markets")).rstrip("/")
        self.key_id_env = str(config.get("key_id_env", "ALPACA_API_KEY_ID"))
        self.secret_key_env = str(config.get("secret_key_env", "ALPACA_API_SECRET_KEY"))

    def readiness(self) -> dict[str, Any]:
        missing = [name for name in (self.key_id_env, self.secret_key_env) if not os.getenv(name)]
        return {"ready": bool(self.config.get("enabled", False)) and not missing, "enabled": bool(self.config.get("enabled", False)), "missing_env": missing}

    def fetch_quotes(
        self,
        symbols: list[str],
        *,
        liquidity_usd: dict[str, float | None] | None = None,
        asset_classes: dict[str, str] | None = None,
    ) -> dict[str, Quote]:
        readiness = self.readiness()
        if not readiness["enabled"]:
            raise AdapterConfigurationError("Alpaca forward quote adapter is disabled")
        if readiness["missing_env"]:
            raise AdapterConfigurationError("missing Alpaca environment variables: " + ", ".join(readiness["missing_env"]))
        normalized = sorted({symbol.strip().upper() for symbol in symbols if symbol.strip()})
        if not normalized:
            return {}
        query = urlencode({"symbols": ",".join(normalized), "feed": str(self.config.get("feed", "iex"))})
        payload = request_json(
            f"{self.base_url}/v2/stocks/snapshots?{query}",
            headers={
                "APCA-API-KEY-ID": str(os.environ[self.key_id_env]),
                "APCA-API-SECRET-KEY": str(os.environ[self.secret_key_env]),
            },
            timeout_seconds=float(self.config.get("timeout_seconds", 20)),
            max_retries=int(self.config.get("max_retries", 2)),
        )
        snapshots = payload.get("snapshots", payload)
        if not isinstance(snapshots, dict):
            raise AdapterDataError("Alpaca snapshots response is not a symbol mapping")
        quotes: dict[str, Quote] = {}
        for symbol in normalized:
            snapshot = snapshots.get(symbol)
            if not isinstance(snapshot, dict):
                continue
            raw_quote = snapshot.get("latestQuote") or snapshot.get("latest_quote") or {}
            raw_trade = snapshot.get("latestTrade") or snapshot.get("latest_trade") or {}
            daily_bar = snapshot.get("dailyBar") or snapshot.get("daily_bar") or {}
            previous_bar = snapshot.get("prevDailyBar") or snapshot.get("previousDailyBar") or snapshot.get("previous_daily_bar") or {}
            bid = self._number(raw_quote, "bp", "bid_price", "bid")
            ask = self._number(raw_quote, "ap", "ask_price", "ask")
            asof = self._text(raw_quote, "t", "timestamp")
            last = self._number(raw_trade, "p", "price", default=(bid + ask) / 2 if bid > 0 and ask > 0 else 0)
            if bid <= 0 or ask <= 0 or ask < bid or not asof:
                continue
            quotes[symbol] = Quote(
                symbol=symbol,
                bid=bid,
                ask=ask,
                last=last,
                asof=asof,
                source=f"alpaca:{self.config.get('feed', 'iex')}",
                avg_daily_volume_usd=(liquidity_usd or {}).get(symbol),
                asset_class=(asset_classes or {}).get(symbol, "us_equity"),
                session_volume=self._number(daily_bar, "v", "volume", default=0) or None,
                previous_close=self._number(previous_bar, "c", "close", default=0) or None,
            )
        if not quotes:
            raise AdapterDataError("Alpaca returned no usable bid/ask quotes")
        return quotes

    @staticmethod
    def _number(payload: dict[str, Any], *keys: str, default: float = 0.0) -> float:
        for key in keys:
            if payload.get(key) is not None:
                return float(payload[key])
        return float(default)

    @staticmethod
    def _text(payload: dict[str, Any], *keys: str) -> str:
        for key in keys:
            if payload.get(key):
                return str(payload[key])
        return ""
