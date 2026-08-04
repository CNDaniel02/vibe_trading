from __future__ import annotations

import asyncio
import re
from datetime import timedelta
from pathlib import Path
from typing import Any

from scripts.adapters.errors import AdapterConfigurationError, AdapterDataError
from scripts.adapters.robinhood_mcp_market_data_adapter import RobinhoodMcpMarketDataAdapter
from scripts.broker.robinhood_mcp_audit import RobinhoodMcpCapabilityClient
from scripts.core.models import Quote, parse_ts


_SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z0-9.-]{0,9}$")
_LEVERAGED_OR_INVERSE_TERMS = (
    "2X",
    "3X",
    "ULTRA",
    "ULTRAPRO",
    "BEAR",
    "INVERSE",
    "SHORT ",
    "-1X",
    "-2X",
    "-3X",
)


class RobinhoodDiscoveryAdapter:
    """Strictly read-only market discovery over explicit Robinhood MCP tools."""

    def __init__(self, config: dict[str, Any], discovery_config: dict[str, Any], root: str | Path) -> None:
        self.config = config
        self.discovery_config = discovery_config
        self.client = RobinhoodMcpCapabilityClient(config, root=root)

    def readiness(self) -> dict[str, Any]:
        return RobinhoodMcpMarketDataAdapter(self.config, root=self.client.store.path.parents[1]).readiness()

    def collect_seed_candidates(self, decision_time: str, core_watchlist: list[str]) -> list[dict[str, Any]]:
        seeds: dict[str, dict[str, Any]] = {}
        for symbol in core_watchlist:
            self._merge_seed(seeds, symbol, "core_watchlist", {})

        cutoff = parse_ts(decision_time)
        start_date = (cutoff - timedelta(days=2)).date().isoformat()
        earnings = asyncio.run(self.client.get_high_market_cap_earnings_calendar(start_date, days=5))
        earnings_rows = earnings.get("data", {}).get("results", []) or []
        scored_earnings: list[tuple[float, dict[str, Any]]] = []
        for row in earnings_rows:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol", "")).upper()
            if not self._valid_symbol(symbol):
                continue
            eps = row.get("eps", {}) if isinstance(row.get("eps"), dict) else {}
            actual = self._number(eps.get("actual"))
            estimate = self._number(eps.get("estimate"))
            surprise = None
            if actual is not None and estimate is not None:
                surprise = (actual - estimate) / max(abs(estimate), 0.05)
            score = abs(surprise) if surprise is not None else 0.0
            scored_earnings.append((score, {**row, "eps_surprise_ratio": surprise}))
        scored_earnings.sort(key=lambda item: item[0], reverse=True)
        for _, row in scored_earnings[: int(self.discovery_config.get("max_earnings_candidates", 12))]:
            self._merge_seed(seeds, str(row["symbol"]), "earnings_calendar", row)

        scans = asyncio.run(self.client.get_scans())
        scan_rows = scans.get("data", {}).get("scans", []) or []
        for scan in scan_rows[: int(self.discovery_config.get("max_saved_scans", 3))]:
            if not isinstance(scan, dict) or not scan.get("id"):
                continue
            result = asyncio.run(self.client.run_scan(str(scan["id"])))
            for symbol in self._symbols_from_scan(result):
                self._merge_seed(
                    seeds,
                    symbol,
                    "saved_robinhood_scan",
                    {"scan_id": str(scan["id"]), "scan_title": str(scan.get("title", ""))},
                )
        return list(seeds.values())

    def fetch_market_context(self, symbols: list[str], decision_time: str) -> dict[str, dict[str, Any]]:
        normalized = sorted({symbol.upper() for symbol in symbols if self._valid_symbol(symbol.upper())})
        if "SPY" not in normalized:
            normalized.append("SPY")
        if not normalized:
            return {}

        fundamentals: dict[str, dict[str, Any]] = {}
        quote_payloads: list[dict[str, Any]] = []
        historicals: dict[str, list[dict[str, Any]]] = {}
        start_time = (parse_ts(decision_time) - timedelta(days=40)).isoformat()
        for batch in self._batches(normalized, 10):
            payload = asyncio.run(self.client.get_equity_fundamentals(batch))
            for row in payload.get("data", {}).get("results", []) or []:
                if isinstance(row, dict) and row.get("symbol"):
                    fundamentals[str(row["symbol"]).upper()] = row
            history = asyncio.run(
                self.client.get_equity_historicals(
                    batch,
                    start_time,
                    decision_time,
                    interval="day",
                )
            )
            for row in history.get("data", {}).get("results", []) or []:
                if isinstance(row, dict) and row.get("symbol"):
                    historicals[str(row["symbol"]).upper()] = list(row.get("bars", []) or [])
        for batch in self._batches(normalized, 20):
            quote_payloads.append(asyncio.run(self.client.get_equity_quotes(batch)))

        liquidity: dict[str, float | None] = {}
        for symbol, row in fundamentals.items():
            average_volume = self._number(row.get("average_volume_30_days") or row.get("average_volume"))
            last = self._last_close(historicals.get(symbol, []))
            liquidity[symbol] = average_volume * last if average_volume is not None and last is not None else None
        quotes: dict[str, Quote] = {}
        for payload in quote_payloads:
            try:
                quotes.update(RobinhoodMcpMarketDataAdapter._parse_quotes(payload, liquidity, {}))
            except AdapterDataError:
                continue

        spy_returns = self._returns(historicals.get("SPY", []))
        contexts: dict[str, dict[str, Any]] = {}
        excluded = {str(value).upper() for value in self.discovery_config.get("excluded_symbols", [])}
        for symbol in normalized:
            if symbol == "SPY" and symbol not in symbols:
                continue
            quote = quotes.get(symbol)
            fundamental = fundamentals.get(symbol)
            if quote is None or fundamental is None or symbol in excluded:
                continue
            returns = self._returns(historicals.get(symbol, []))
            volume = self._number(fundamental.get("volume"))
            average_volume = self._number(fundamental.get("average_volume_30_days") or fundamental.get("average_volume"))
            volume_ratio = volume / average_volume if volume is not None and average_volume and average_volume > 0 else None
            market_cap = self._number(fundamental.get("market_cap"))
            avg_daily_volume_usd = liquidity.get(symbol)
            quote.avg_daily_volume_usd = avg_daily_volume_usd
            eligible = (
                market_cap is not None
                and market_cap >= float(self.discovery_config.get("minimum_market_cap_usd", 0))
                and avg_daily_volume_usd is not None
                and avg_daily_volume_usd >= float(self.discovery_config.get("minimum_average_daily_volume_usd", 0))
                and quote.spread_bps() <= float(self.discovery_config.get("maximum_spread_bps", 50))
            )
            contexts[symbol] = {
                "ticker": symbol,
                "eligible": eligible,
                "quote": quote.to_dict(),
                "fundamentals": {
                    "market_cap": market_cap,
                    "average_daily_volume_usd": avg_daily_volume_usd,
                    "volume_ratio": volume_ratio,
                    "sector": fundamental.get("sector"),
                    "industry": fundamental.get("industry"),
                    "pe_ratio": self._number(fundamental.get("pe_ratio")),
                    "financial_status_description": fundamental.get("financial_status_description"),
                },
                "technical_signals": {
                    "price_change_1d_pct": returns["1d"],
                    "price_change_5d_pct": returns["5d"],
                    "price_change_20d_pct": returns["20d"],
                    "relative_strength_20d": (
                        returns["20d"] - spy_returns["20d"]
                        if returns["20d"] is not None and spy_returns["20d"] is not None
                        else None
                    ),
                    "volume_ratio": volume_ratio,
                    "spread_bps": quote.spread_bps(),
                },
                "source": "robinhood_mcp",
                "data_cutoff_time": decision_time,
            }
        return contexts

    def validate_instrument(self, symbol: str) -> dict[str, Any]:
        payload = asyncio.run(self.client.search_instruments(symbol, limit=5))
        results = payload.get("data", {}).get("results", []) or []
        exact = next(
            (
                row
                for row in results
                if isinstance(row, dict) and str(row.get("symbol", "")).upper() == symbol.upper()
            ),
            None,
        )
        if exact is None:
            return {"valid": False, "reason": "no exact US-listed Robinhood instrument match"}
        name = str(exact.get("name") or exact.get("simple_name") or "")
        upper_name = name.upper()
        if any(term in upper_name for term in _LEVERAGED_OR_INVERSE_TERMS):
            return {"valid": False, "reason": "leveraged or inverse product excluded", "name": name}
        return {
            "valid": True,
            "reason": "exact US-listed instrument match",
            "name": name,
            "instrument_id": exact.get("instrument_id"),
        }

    def fetch_current_quote(
        self,
        symbol: str,
        *,
        average_daily_volume_usd: float | None,
        asset_class: str = "us_equity",
    ) -> Quote:
        payload = asyncio.run(self.client.get_equity_quotes([symbol]))
        quotes = RobinhoodMcpMarketDataAdapter._parse_quotes(
            payload,
            {symbol: average_daily_volume_usd},
            {symbol: asset_class},
        )
        quote = quotes.get(symbol)
        if quote is None:
            raise AdapterDataError(f"Robinhood returned no current quote for {symbol}")
        return quote

    @staticmethod
    def _merge_seed(target: dict[str, dict[str, Any]], symbol: str, source: str, detail: dict[str, Any]) -> None:
        normalized = symbol.strip().upper()
        if not RobinhoodDiscoveryAdapter._valid_symbol(normalized):
            return
        item = target.setdefault(normalized, {"ticker": normalized, "sources": [], "source_details": []})
        if source not in item["sources"]:
            item["sources"].append(source)
        item["source_details"].append({"source": source, "detail": detail})

    @staticmethod
    def _symbols_from_scan(payload: dict[str, Any]) -> list[str]:
        data = payload.get("data", {})
        rows = data.get("results") or data.get("rows") or data.get("instruments") or []
        symbols: list[str] = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("ticker") or row.get("symbol") or "").upper()
            if RobinhoodDiscoveryAdapter._valid_symbol(symbol):
                symbols.append(symbol)
        return symbols

    @staticmethod
    def _returns(bars: list[dict[str, Any]]) -> dict[str, float | None]:
        closes = [
            float(row["close_price"])
            for row in sorted(bars, key=lambda item: str(item.get("begins_at", "")))
            if isinstance(row, dict) and row.get("close_price") is not None and not row.get("interpolated", False)
        ]

        def change(period: int) -> float | None:
            if len(closes) <= period or closes[-period - 1] <= 0:
                return None
            return round((closes[-1] / closes[-period - 1] - 1) * 100, 4)

        return {"1d": change(1), "5d": change(5), "20d": change(20)}

    @staticmethod
    def _last_close(bars: list[dict[str, Any]]) -> float | None:
        values = [
            (str(row.get("begins_at", "")), RobinhoodDiscoveryAdapter._number(row.get("close_price")))
            for row in bars
            if isinstance(row, dict) and not row.get("interpolated", False)
        ]
        values = [(stamp, value) for stamp, value in values if value is not None]
        return sorted(values)[-1][1] if values else None

    @staticmethod
    def _number(value: Any) -> float | None:
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _valid_symbol(symbol: str) -> bool:
        return bool(_SYMBOL_PATTERN.fullmatch(symbol))

    @staticmethod
    def _batches(values: list[str], size: int) -> list[list[str]]:
        return [values[index : index + size] for index in range(0, len(values), size)]
