from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from scripts.adapters.errors import AdapterConfigurationError, AdapterDataError, summarize_external_error
from scripts.broker.robinhood_mcp_audit import RobinhoodMcpCapabilityClient
from scripts.core.models import Quote


class RobinhoodMcpMarketDataAdapter:
    """Read-only Robinhood MCP quote adapter for the low-frequency shadow loop.

    It supports only ``get_equity_quotes`` and has no order, portfolio, or
    generic MCP call surface. Historical/replay and streaming workloads remain
    outside this adapter's scope and continue to belong to Alpaca or Vibe.
    """

    def __init__(self, config: dict[str, Any], root: str | Path | None = None) -> None:
        self.config = config
        self.client = RobinhoodMcpCapabilityClient(config, root=root, interactive_oauth=False)

    def _local_readiness(self) -> dict[str, Any]:
        if not self.config.get("enabled", False):
            return {"ready": False, "enabled": False, "missing_env": [], "reason": "Robinhood MCP quote adapter is disabled"}
        try:
            token_present = asyncio.run(self.client.store.get_tokens()) is not None
            registration_present = asyncio.run(self.client.store.get_client_info()) is not None
        except Exception as exc:
            return {"ready": False, "enabled": True, "missing_env": [], "reason": f"credential store unavailable: {type(exc).__name__}"}
        return {
            "ready": token_present and registration_present,
            "enabled": True,
            "missing_env": [],
            "reason": "ready" if token_present and registration_present else "run the Python Robinhood MCP OAuth capability audit first",
        }

    def readiness(self, *, live_probe: bool = True) -> dict[str, Any]:
        local = self._local_readiness()
        if not local["ready"] or not live_probe:
            return local
        try:
            probe = asyncio.run(self.client.probe())
        except Exception as exc:
            return {
                **local,
                "ready": False,
                "reason": summarize_external_error(exc),
                "live_probe": False,
            }
        tool_available = bool(probe.get("readonly_quote_tool_available"))
        return {
            **local,
            "ready": tool_available,
            "reason": "ready" if tool_available else "Robinhood read-only quote tool is unavailable",
            "live_probe": True,
            "tool_count": probe.get("tool_count"),
        }

    def fetch_quotes(
        self,
        symbols: list[str],
        *,
        liquidity_usd: dict[str, float | None] | None = None,
        asset_classes: dict[str, str] | None = None,
    ) -> dict[str, Quote]:
        readiness = self._local_readiness()
        if not readiness["enabled"]:
            raise AdapterConfigurationError("Robinhood MCP forward quote adapter is disabled")
        if not readiness["ready"]:
            raise AdapterConfigurationError(str(readiness["reason"]))
        quotes: dict[str, Quote] = {}
        for batch in self._batches(symbols, 20):
            payload = self._run(
                self.client.get_equity_quotes(batch),
                "get_equity_quotes",
            )
            quotes.update(
                self._parse_quotes(
                    payload,
                    liquidity_usd or {},
                    asset_classes or {},
                )
            )
        return quotes

    def fetch_session_volumes(self, symbols: list[str], start_time: str, end_time: str) -> dict[str, float]:
        readiness = self._local_readiness()
        if not readiness["ready"]:
            raise AdapterConfigurationError(str(readiness["reason"]))
        volumes: dict[str, float] = {}
        for batch in self._batches(symbols, 10):
            payload = self._run(
                self.client.get_equity_historicals(
                    batch,
                    start_time,
                    end_time,
                ),
                "get_equity_historicals",
            )
            for result in payload.get("data", {}).get("results", []) or []:
                if not isinstance(result, dict):
                    continue
                symbol = str(result.get("symbol", "")).upper()
                bars = result.get("bars", []) or []
                if not symbol or not isinstance(bars, list):
                    continue
                volumes[symbol] = float(
                    sum(
                        int(bar.get("volume", 0))
                        for bar in bars
                        if isinstance(bar, dict)
                        and not bar.get("interpolated", False)
                    )
                )
        return volumes

    @staticmethod
    def _batches(values: list[str], size: int) -> list[list[str]]:
        normalized = list(
            dict.fromkeys(value.upper() for value in values if value)
        )
        return [
            normalized[index : index + size]
            for index in range(0, len(normalized), size)
        ]

    @staticmethod
    def _run(coro: Any, operation: str) -> dict[str, Any]:
        try:
            return asyncio.run(coro)
        except TimeoutError as exc:
            raise AdapterDataError(f"Robinhood MCP {operation} timed out") from exc
        except Exception as exc:
            raise AdapterDataError(
                f"Robinhood MCP {operation} failed: {summarize_external_error(exc)}"
            ) from exc

    @staticmethod
    def _parse_quotes(
        payload: dict[str, Any],
        liquidity_usd: dict[str, float | None],
        asset_classes: dict[str, str],
    ) -> dict[str, Quote]:
        records = payload.get("data", {}).get("results", [])
        if not isinstance(records, list):
            raise AdapterDataError("Robinhood quote response is not a results list")
        quotes: dict[str, Quote] = {}
        for record in records:
            if not isinstance(record, dict):
                continue
            raw = record.get("quote") or {}
            symbol = str(raw.get("symbol") or "").upper()
            bid = RobinhoodMcpMarketDataAdapter._number(raw.get("bid_price"))
            ask = RobinhoodMcpMarketDataAdapter._number(raw.get("ask_price"))
            last = RobinhoodMcpMarketDataAdapter._number(raw.get("last_trade_price"), default=(bid + ask) / 2 if bid and ask else 0)
            asof = str(raw.get("venue_bid_time") or raw.get("venue_ask_time") or "")
            state = str(raw.get("state") or "").lower()
            if not symbol or bid <= 0 or ask <= 0 or ask < bid or not asof or state != "active":
                continue
            quotes[symbol] = Quote(
                symbol=symbol,
                bid=bid,
                ask=ask,
                last=last,
                asof=asof,
                source="robinhood_mcp:get_equity_quotes",
                avg_daily_volume_usd=liquidity_usd.get(symbol),
                asset_class=asset_classes.get(symbol, "us_equity"),
                previous_close=RobinhoodMcpMarketDataAdapter._number(raw.get("adjusted_previous_close"), default=0) or None,
            )
        if not quotes:
            raise AdapterDataError("Robinhood returned no usable active bid/ask quotes")
        return quotes

    @staticmethod
    def _number(value: Any, default: float = 0.0) -> float:
        try:
            return float(value) if value is not None else float(default)
        except (TypeError, ValueError):
            return float(default)
