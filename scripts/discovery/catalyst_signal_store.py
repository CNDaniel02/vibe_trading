from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

from scripts.core.models import parse_ts, utc_now
from scripts.core.state import JsonStateStore


class CatalystSignalStore:
    FILENAME = "catalyst_signals.json"

    def __init__(self, root: str | Path) -> None:
        self.store = JsonStateStore(root)

    def put(
        self,
        ticker: str,
        signal: dict[str, Any],
        *,
        observed_at: str,
        ttl_hours: int = 48,
    ) -> None:
        values = self.store.read_json(self.FILENAME, {})
        values[ticker.upper()] = {
            **signal,
            "ticker": ticker.upper(),
            "observed_at": observed_at,
            "expires_at": (parse_ts(observed_at) + timedelta(hours=ttl_hours)).isoformat(),
            "updated_at": utc_now(),
        }
        self.store.write_json(self.FILENAME, values)

    def get(self, ticker: str, asof: str) -> dict[str, Any] | None:
        values = self.store.read_json(self.FILENAME, {})
        signal = values.get(ticker.upper())
        if not isinstance(signal, dict):
            return None
        observed = signal.get("observed_at")
        expires = signal.get("expires_at")
        if not observed or not expires:
            return None
        now = parse_ts(asof)
        if parse_ts(str(observed)) > now or now > parse_ts(str(expires)):
            return None
        return dict(signal)
