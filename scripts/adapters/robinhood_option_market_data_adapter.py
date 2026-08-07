from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from scripts.adapters.errors import AdapterConfigurationError, AdapterDataError, summarize_external_error
from scripts.broker.robinhood_mcp_audit import RobinhoodMcpCapabilityClient
from scripts.core.models import parse_ts, utc_now
from scripts.options.models import OptionContract, OptionQuote
from scripts.options.selection import rank_contracts, rank_contracts_with_diagnostics


class RobinhoodOptionMarketDataAdapter:
    """Read-only option chain and quote adapter with no order-call surface."""

    REQUIRED_TOOLS = frozenset(
        {
            "get_option_chains",
            "get_option_instruments",
            "get_option_quotes",
            "get_earnings_calendar",
        }
    )

    def __init__(self, config: dict[str, Any], runtime_config: dict[str, Any], root: str | Path) -> None:
        self.config = config
        self.runtime_config = runtime_config
        self.client = RobinhoodMcpCapabilityClient(config, root=root, interactive_oauth=False)

    def readiness(self, *, live_probe: bool = True) -> dict[str, Any]:
        if not self.config.get("enabled", False):
            return {"ready": False, "reason": "Robinhood MCP disabled"}
        try:
            ready = asyncio.run(self.client.store.get_tokens()) is not None and asyncio.run(self.client.store.get_client_info()) is not None
        except Exception as exc:
            return {"ready": False, "reason": f"credential store unavailable: {type(exc).__name__}"}
        if not ready or not live_probe:
            return {"ready": ready, "reason": "ready" if ready else "Robinhood MCP OAuth is not initialized"}
        try:
            probe = asyncio.run(self.client.probe())
        except Exception as exc:
            return {"ready": False, "reason": summarize_external_error(exc), "live_probe": False}
        available = set(probe.get("tool_names", []))
        missing = sorted(self.REQUIRED_TOOLS - available)
        return {
            "ready": not missing,
            "reason": "ready" if not missing else "required Robinhood option tools are unavailable",
            "live_probe": True,
            "missing_tools": missing,
        }

    def _require_local_ready(self) -> None:
        status = self.readiness(live_probe=False)
        if not status["ready"]:
            raise AdapterConfigurationError(str(status["reason"]))

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

    def fetch_best_contract(
        self,
        *,
        underlying: str,
        underlying_price: float,
        option_type: str,
        now: str,
        max_premium_usd: float | None = None,
    ) -> tuple[OptionContract, OptionQuote] | None:
        self._require_local_ready()
        chain_payload = self._run(self.client.get_option_chains(underlying), "get_option_chains")
        chain = self._select_chain(chain_payload, underlying)
        expiration = self._select_expiration(chain, now)
        instruments = self._fetch_instruments(chain, expiration, option_type)
        cap = int(self.runtime_config["options_universe"].get("max_contracts_considered_per_side", 20))
        instruments.sort(key=lambda item: abs(float(item.get("strike_price", 0)) - underlying_price))
        instruments = instruments[: min(cap, 20)]
        contracts = [self._parse_contract(item, chain) for item in instruments]
        quote_payload = self._run(
            self.client.get_option_quotes([item.option_id for item in contracts]),
            "get_option_quotes",
        )
        quotes = self._parse_quotes(quote_payload)
        # The decision time precedes the network request. Validate returned
        # quotes against the local observation time after the response.
        quotes_observed_at = utc_now(timespec="microseconds")
        ranked = rank_contracts(
            contracts,
            quotes,
            quotes_observed_at,
            self.runtime_config,
        )
        if max_premium_usd is not None:
            ranked = [
                item
                for item in ranked
                if item[1].ask * item[0].multiplier <= max_premium_usd
            ]
        return ranked[0] if ranked else None

    def upcoming_earnings(self, symbols: list[str], now: str, days: int = 7) -> dict[str, dict[str, Any]]:
        self._require_local_ready()
        payload = self._run(
            self.client.get_earnings_calendar(parse_ts(now).date().isoformat(), days),
            "get_earnings_calendar",
        )
        wanted = {symbol.upper() for symbol in symbols}
        results: dict[str, dict[str, Any]] = {}
        for item in payload.get("data", {}).get("results", []) or []:
            if isinstance(item, dict) and str(item.get("symbol", "")).upper() in wanted and item.get("report"):
                results[str(item["symbol"]).upper()] = item
        return results

    def fetch_best_contract_with_diagnostics(
        self,
        *,
        underlying: str,
        underlying_price: float,
        option_type: str,
        now: str,
        max_premium_usd: float | None = None,
    ) -> tuple[tuple[OptionContract, OptionQuote] | None, dict[str, Any]]:
        self._require_local_ready()
        chain_payload = self._run(self.client.get_option_chains(underlying), "get_option_chains")
        chain = self._select_chain(chain_payload, underlying)
        expiration = self._select_expiration(chain, now)
        instruments = self._fetch_instruments(chain, expiration, option_type)
        cap = int(self.runtime_config["options_universe"].get("max_contracts_considered_per_side", 20))
        instruments.sort(key=lambda item: abs(float(item.get("strike_price", 0)) - underlying_price))
        instruments = instruments[: min(cap, 20)]
        contracts = [self._parse_contract(item, chain) for item in instruments]
        quote_payload = self._run(
            self.client.get_option_quotes([item.option_id for item in contracts]),
            "get_option_quotes",
        )
        quotes = self._parse_quotes(quote_payload)
        quotes_observed_at = utc_now(timespec="microseconds")
        ranked, diagnostics = rank_contracts_with_diagnostics(
            contracts,
            quotes,
            quotes_observed_at,
            self.runtime_config,
        )
        diagnostics.update(
            {
                "underlying": underlying,
                "option_type": option_type,
                "expiration": expiration,
                "max_premium_usd": max_premium_usd,
                "selection_started_at": now,
                "quotes_observed_at": quotes_observed_at,
            }
        )
        cheapest = min(
            ranked,
            key=lambda item: item[1].ask * item[0].multiplier,
            default=None,
        )
        if cheapest is not None:
            contract, quote = cheapest
            minimum_premium = quote.ask * contract.multiplier
            diagnostics["minimum_eligible_premium_usd"] = round(minimum_premium, 4)
            diagnostics["cheapest_eligible_contract"] = {
                "option_id": contract.option_id,
                "strike_price": contract.strike_price,
                "option_type": contract.option_type,
                "ask": quote.ask,
                "delta": quote.delta,
                "volume": quote.volume,
                "open_interest": quote.open_interest,
                "spread_pct": round(quote.spread_pct(), 6),
            }
            if max_premium_usd is not None:
                diagnostics["minimum_budget_shortfall_usd"] = round(
                    max(0.0, minimum_premium - max_premium_usd),
                    4,
                )
        if max_premium_usd is not None:
            before = len(ranked)
            ranked = [
                item
                for item in ranked
                if item[1].ask * item[0].multiplier <= max_premium_usd
            ]
            diagnostics["rejections"]["premium above deterministic budget"] = before - len(ranked)
        diagnostics["accepted_after_premium_cap"] = len(ranked)
        return (ranked[0] if ranked else None), diagnostics

    def fetch_quotes(self, option_ids: list[str]) -> dict[str, OptionQuote]:
        if not option_ids:
            return {}
        self._require_local_ready()
        return self._parse_quotes(self._run(self.client.get_option_quotes(option_ids), "get_option_quotes"))

    def _select_chain(self, payload: dict[str, Any], underlying: str) -> dict[str, Any]:
        chains = payload.get("data", {}).get("chains", []) or []
        candidates = [
            item
            for item in chains
            if isinstance(item, dict)
            and item.get("can_open_position")
            and int(float(item.get("trade_value_multiplier", 0))) == 100
            and str(item.get("symbol", "")).upper() == underlying.upper()
            and bool(item.get("underlying_instruments", []) or [])
        ]
        if not candidates:
            raise AdapterDataError(f"no supported openable equity option chain for {underlying}")
        candidates.sort(key=lambda item: (bool(item.get("settle_on_open")), str(item.get("id"))))
        return candidates[0]

    def _select_expiration(self, chain: dict[str, Any], now: str) -> str:
        universe = self.runtime_config["options_universe"]
        current = parse_ts(now).date()
        minimum = int(universe.get("min_dte", 21))
        maximum = int(universe.get("max_dte", 45))
        target = int(universe.get("target_dte", 30))
        expirations = []
        for value in chain.get("expiration_dates", []) or []:
            dte = (date.fromisoformat(str(value)) - current).days
            if minimum <= dte <= maximum:
                expirations.append((abs(dte - target), dte, str(value)))
        if not expirations:
            raise AdapterDataError("no option expiration inside configured DTE window")
        return min(expirations)[2]

    def _fetch_instruments(self, chain: dict[str, Any], expiration: str, option_type: str) -> list[dict[str, Any]]:
        instruments: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(5):
            payload = self._run(
                self.client.get_option_instruments(
                    chain_id=str(chain["id"]),
                    expiration_date=expiration,
                    option_type=option_type,
                    cursor=cursor,
                ),
                "get_option_instruments",
            )
            instruments.extend(item for item in payload.get("data", {}).get("instruments", []) or [] if isinstance(item, dict))
            next_url = str(payload.get("data", {}).get("next") or "")
            cursor = parse_qs(urlparse(next_url).query).get("cursor", [None])[0] if next_url else None
            if not cursor:
                break
        return instruments

    @staticmethod
    def _parse_contract(item: dict[str, Any], chain: dict[str, Any]) -> OptionContract:
        ticks = item.get("min_ticks") or chain.get("min_ticks") or {}
        return OptionContract(
            option_id=str(item["id"]),
            chain_id=str(item["chain_id"]),
            underlying=str(item["chain_symbol"]).upper(),
            option_type=str(item["type"]),  # type: ignore[arg-type]
            strike_price=float(item["strike_price"]),
            expiration_date=str(item["expiration_date"]),
            multiplier=int(float(chain["trade_value_multiplier"])),
            sellout_datetime=str(item.get("sellout_datetime") or "") or None,
            below_tick=float(ticks.get("below_tick", 0.01)),
            above_tick=float(ticks.get("above_tick", 0.05)),
            tick_cutoff_price=float(ticks.get("cutoff_price", 3.0)),
        )

    @staticmethod
    def _parse_quotes(payload: dict[str, Any]) -> dict[str, OptionQuote]:
        quotes: dict[str, OptionQuote] = {}
        for result in payload.get("data", {}).get("results", []) or []:
            raw = result.get("quote") if isinstance(result, dict) else None
            if not isinstance(raw, dict):
                continue
            option_id = str(raw.get("instrument_id") or "")
            if not option_id:
                continue
            quotes[option_id] = OptionQuote(
                option_id=option_id,
                bid=float(raw.get("bid_price") or 0),
                ask=float(raw.get("ask_price") or 0),
                mark=float(raw.get("mark_price") or 0),
                updated_at=str(raw.get("updated_at") or ""),
                source="robinhood_mcp:get_option_quotes",
                delta=_optional_float(raw.get("delta")),
                gamma=_optional_float(raw.get("gamma")),
                theta=_optional_float(raw.get("theta")),
                vega=_optional_float(raw.get("vega")),
                rho=_optional_float(raw.get("rho")),
                implied_volatility=_optional_float(raw.get("implied_volatility")),
                volume=int(raw.get("volume") or 0),
                open_interest=int(raw.get("open_interest") or 0),
                bid_size=int(raw.get("bid_size") or 0),
                ask_size=int(raw.get("ask_size") or 0),
                chance_of_profit_long=_optional_float(raw.get("chance_of_profit_long")),
            )
        return quotes


def _optional_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
