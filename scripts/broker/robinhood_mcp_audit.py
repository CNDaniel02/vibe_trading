"""OAuth-backed, read-only Robinhood MCP capability audit.

This module intentionally exposes only a capability audit and narrowly
whitelisted read-only market-data requests. It cannot place, cancel, review,
or mutate broker orders, and it has no generic public tool-call method.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, AsyncIterator
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import httpx
import win32crypt
from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
from pydantic import AnyUrl

from scripts.core.config import PROJECT_ROOT, load_runtime_config


ROBINHOOD_TRADING_MCP_URL = "https://agent.robinhood.com/mcp/trading"

# Captured from the current authenticated MCP runtime. The audit treats absent
# names as a failure and surfaces additions for manual review rather than
# assuming a new server capability is safe.
EXPECTED_ROBINHOOD_TOOLS = frozenset(
    {
        "add_option_to_watchlist",
        "add_to_watchlist",
        "cancel_equity_order",
        "cancel_option_order",
        "create_scan",
        "create_watchlist",
        "follow_watchlist",
        "get_accounts",
        "get_earnings_calendar",
        "get_earnings_results",
        "get_equity_fundamentals",
        "get_equity_historicals",
        "get_equity_orders",
        "get_equity_positions",
        "get_equity_price_book",
        "get_equity_quotes",
        "get_equity_tax_lots",
        "get_equity_technical_indicators",
        "get_equity_tradability",
        "get_financials",
        "get_index_quotes",
        "get_indexes",
        "get_option_chains",
        "get_option_historicals",
        "get_option_instruments",
        "get_option_level_upgrade_info",
        "get_option_orders",
        "get_option_positions",
        "get_option_quotes",
        "get_option_watchlist",
        "get_pnl_trade_history",
        "get_popular_watchlists",
        "get_portfolio",
        "get_realized_pnl",
        "get_scanner_filter_specs",
        "get_scans",
        "get_watchlist_items",
        "get_watchlists",
        "place_equity_order",
        "place_option_order",
        "remove_from_watchlist",
        "remove_option_from_watchlist",
        "review_equity_order",
        "review_option_order",
        "run_scan",
        "search",
        "unfollow_watchlist",
        "update_scan_config",
        "update_scan_filters",
        "update_watchlist",
    }
)


class CredentialStore:
    """Persist OAuth material in a current-user DPAPI encrypted local file.

    Robinhood access and refresh tokens can exceed the Windows Credential
    Manager secret-size limit. DPAPI encrypts the full envelope for the current
    Windows user while ``state/`` keeps the file outside version control.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = RLock()

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            _, plaintext = win32crypt.CryptUnprotectData(self.path.read_bytes(), None, None, None, 0)
            value = json.loads(plaintext.decode("utf-8"))
        except Exception as exc:
            raise RuntimeError("unable to decrypt Robinhood OAuth credentials for the current Windows user") from exc
        if not isinstance(value, dict):
            raise RuntimeError("Robinhood OAuth credential envelope is invalid")
        return value

    def _write(self, envelope: dict[str, Any]) -> None:
        plaintext = json.dumps(envelope, separators=(",", ":"), sort_keys=True).encode("utf-8")
        encrypted = win32crypt.CryptProtectData(plaintext, "auto-trading-skill Robinhood MCP OAuth", None, None, None, 0)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        temporary.write_bytes(encrypted)
        temporary.replace(self.path)

    async def get_tokens(self) -> OAuthToken | None:
        with self._lock:
            raw = self._read().get("tokens")
        return OAuthToken.model_validate(raw) if raw else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        with self._lock:
            envelope = self._read()
            envelope["tokens"] = tokens.model_dump(mode="json")
            self._write(envelope)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        with self._lock:
            raw = self._read().get("client_info")
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        with self._lock:
            envelope = self._read()
            envelope["client_info"] = client_info.model_dump(mode="json")
            self._write(envelope)

    def archive_existing(self) -> Path | None:
        """Keep an unreadable credential blob out of the active OAuth path."""
        with self._lock:
            if not self.path.exists():
                return None
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            archive = self.path.with_name(f"{self.path.stem}.archived-{stamp}-{uuid4().hex[:8]}{self.path.suffix}")
            self.path.replace(archive)
            return archive


@dataclass(frozen=True)
class CapabilityAudit:
    endpoint: str
    tool_count: int
    missing_expected: list[str]
    unexpected: list[str]
    tool_names: list[str]

    @property
    def passed(self) -> bool:
        return not self.missing_expected

    def to_dict(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "tool_count": self.tool_count,
            "passed": self.passed,
            "missing_expected": self.missing_expected,
            "unexpected": self.unexpected,
            "tool_names": self.tool_names,
        }


class RobinhoodMcpCapabilityClient:
    """Create an OAuth session and audit only the server's declared tools."""

    def __init__(self, config: dict[str, Any], root: str | Path | None = None) -> None:
        self.endpoint = str(config.get("endpoint", ROBINHOOD_TRADING_MCP_URL))
        base = Path(root).resolve() if root is not None else PROJECT_ROOT
        configured_path = Path(str(config.get("credential_store_path", "state/robinhood_mcp_oauth.dpapi")))
        self.store = CredentialStore(configured_path if configured_path.is_absolute() else base / configured_path)
        self.redirect_uri = str(config.get("redirect_uri", "http://127.0.0.1:8765/callback"))
        self.client_name = str(config.get("client_name", "auto-trading-skill read-only capability audit"))

    async def _show_authorization_url(self, authorization_url: str) -> None:
        print("Open this Robinhood OAuth URL in a desktop browser, approve access, then paste the final callback URL:")
        print(authorization_url)

    async def _read_callback(self) -> tuple[str, str | None]:
        callback_url = input("Robinhood OAuth callback URL: ").strip()
        parsed = urlparse(callback_url)
        expected = urlparse(self.redirect_uri)
        if parsed.scheme != expected.scheme or parsed.netloc != expected.netloc or parsed.path != expected.path:
            raise RuntimeError("callback URL does not match the configured redirect URI")
        params = parse_qs(parsed.query)
        if "error" in params:
            raise RuntimeError("Robinhood OAuth authorization was declined or failed")
        code = params.get("code", [""])[0]
        if not code:
            raise RuntimeError("callback URL did not include an authorization code")
        return code, params.get("state", [None])[0]

    def _oauth(self) -> OAuthClientProvider:
        return OAuthClientProvider(
            server_url=self.endpoint,
            client_metadata=OAuthClientMetadata(
                client_name=self.client_name,
                redirect_uris=[AnyUrl(self.redirect_uri)],
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                token_endpoint_auth_method="none",
            ),
            storage=self.store,
            redirect_handler=self._show_authorization_url,
            callback_handler=self._read_callback,
        )

    @asynccontextmanager
    async def session(self) -> AsyncIterator[ClientSession]:
        oauth = self._oauth()
        async with httpx.AsyncClient(auth=oauth, follow_redirects=True, timeout=60) as http_client:
            # Robinhood accepts the MCP Streamable HTTP session but returns
            # HTTP 400 to the SDK's optional DELETE termination request. Let
            # the server expire the read-only session naturally instead.
            async with streamable_http_client(
                self.endpoint,
                http_client=http_client,
                terminate_on_close=False,
            ) as (read_stream, write_stream, _):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    yield session

    async def audit(self) -> CapabilityAudit:
        async with self.session() as session:
            await session.send_ping()
            result = await session.list_tools()
        # Do not report a successful OAuth audit unless the persisted material
        # can be read back after the MCP connection has closed.
        if await self.store.get_client_info() is None or await self.store.get_tokens() is None:
            raise RuntimeError("Robinhood OAuth credentials were not persisted")
        names = sorted(tool.name for tool in result.tools)
        actual = set(names)
        return CapabilityAudit(
            endpoint=self.endpoint,
            tool_count=len(names),
            missing_expected=sorted(EXPECTED_ROBINHOOD_TOOLS - actual),
            unexpected=sorted(actual - EXPECTED_ROBINHOOD_TOOLS),
            tool_names=names,
        )

    async def get_equity_quotes(self, symbols: list[str]) -> dict[str, Any]:
        """Return only the MCP payload from the read-only quote tool.

        This explicit whitelist is the sole data-call escape hatch for the
        forward shadow service. It deliberately does not accept an arbitrary
        MCP tool name or arbitrary account-bearing arguments.
        """
        normalized = sorted({symbol.strip().upper() for symbol in symbols if symbol.strip()})
        if not normalized:
            return {"data": {"results": []}}
        if len(normalized) > 20:
            raise ValueError("Robinhood get_equity_quotes accepts at most 20 symbols per call")
        async with self.session() as session:
            result = await session.call_tool("get_equity_quotes", arguments={"symbols": normalized})
        payload = result.structuredContent
        if not isinstance(payload, dict):
            raise RuntimeError("Robinhood get_equity_quotes returned no structured payload")
        return payload

    async def get_equity_historicals(self, symbols: list[str], start_time: str, end_time: str) -> dict[str, Any]:
        normalized = sorted({symbol.strip().upper() for symbol in symbols if symbol.strip()})
        if not normalized or len(normalized) > 10:
            raise ValueError("Robinhood get_equity_historicals requires 1 to 10 symbols")
        arguments = {
            "symbols": normalized,
            "start_time": start_time,
            "end_time": end_time,
            "interval": "5minute",
            "bounds": "regular",
            "adjustment_type": "split",
        }
        async with self.session() as session:
            result = await session.call_tool("get_equity_historicals", arguments=arguments)
        return self._structured_payload(result.structuredContent, "get_equity_historicals")

    async def get_option_chains(self, underlying_symbol: str) -> dict[str, Any]:
        symbol = underlying_symbol.strip().upper()
        if not symbol:
            raise ValueError("underlying_symbol is required")
        async with self.session() as session:
            result = await session.call_tool("get_option_chains", arguments={"underlying_symbol": symbol})
        return self._structured_payload(result.structuredContent, "get_option_chains")

    async def get_option_instruments(
        self,
        *,
        chain_id: str,
        expiration_date: str,
        option_type: str,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        if option_type not in {"call", "put"}:
            raise ValueError("option_type must be call or put")
        arguments = {
            "chain_id": chain_id,
            "expiration_dates": expiration_date,
            "type": option_type,
            "state": "active",
            "tradability": "tradable",
        }
        if cursor:
            arguments["cursor"] = cursor
        async with self.session() as session:
            result = await session.call_tool("get_option_instruments", arguments=arguments)
        return self._structured_payload(result.structuredContent, "get_option_instruments")

    async def get_option_quotes(self, option_ids: list[str]) -> dict[str, Any]:
        normalized = list(dict.fromkeys(value.strip() for value in option_ids if value.strip()))
        if not normalized:
            return {"data": {"results": []}}
        if len(normalized) > 20:
            raise ValueError("Robinhood option quote batches are limited to 20 contracts")
        async with self.session() as session:
            result = await session.call_tool("get_option_quotes", arguments={"instrument_ids": normalized})
        return self._structured_payload(result.structuredContent, "get_option_quotes")

    async def get_earnings_calendar(self, start_date: str, days: int = 7) -> dict[str, Any]:
        if days == 0 or not -31 <= days <= 31:
            raise ValueError("earnings calendar days must be between -31 and 31 and non-zero")
        async with self.session() as session:
            result = await session.call_tool("get_earnings_calendar", arguments={"start_date": start_date, "days": days})
        return self._structured_payload(result.structuredContent, "get_earnings_calendar")

    @staticmethod
    def _structured_payload(payload: Any, tool_name: str) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise RuntimeError(f"Robinhood {tool_name} returned no structured payload")
        return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a read-only Robinhood MCP capability audit.")
    parser.add_argument("--root", default=".")
    parser.add_argument(
        "--reset-credentials",
        action="store_true",
        help="archive existing local OAuth credentials and require a fresh browser authorization",
    )
    args = parser.parse_args()
    root = Path(args.root).resolve()
    config = load_runtime_config(root).get("integrations", {}).get("robinhood_mcp", {})
    client = RobinhoodMcpCapabilityClient(config, root=root)
    if args.reset_credentials:
        archived = client.store.archive_existing()
        if archived:
            print(f"Archived existing OAuth credential file: {archived}")
    audit = asyncio.run(client.audit())
    print(json.dumps(audit.to_dict(), indent=2, sort_keys=True))
    if not audit.passed:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
