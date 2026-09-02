"""OAuth-backed, read-only Robinhood MCP capability audit.

This module intentionally exposes only a capability audit and narrowly
whitelisted read-only market-data requests. It cannot place, cancel, review,
or mutate broker orders, and it has no generic public tool-call method.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import math
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, AsyncIterator, Iterator, TypeVar
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import httpx
import win32crypt
from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthMetadata,
    OAuthToken,
)
from pydantic import AnyUrl

from scripts.core.config import PROJECT_ROOT, load_runtime_config
from scripts.core.file_lock import InterProcessFileLock


ROBINHOOD_TRADING_MCP_URL = "https://agent.robinhood.com/mcp/trading"
TOKEN_REFRESH_SKEW_SECONDS = 60
AUTH_RECONNECT_COOLDOWN_SECONDS = 300
MAX_AUTH_RECONNECT_LOCK_TIMEOUT_SECONDS = 5.0
T = TypeVar("T")


def _validated_robinhood_oauth_metadata(
    metadata: OAuthMetadata,
) -> OAuthMetadata:
    for field in (
        "issuer",
        "authorization_endpoint",
        "token_endpoint",
        "registration_endpoint",
    ):
        value = getattr(metadata, field)
        if value is None:
            continue
        parsed = urlparse(str(value))
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not (
            host == "robinhood.com" or host.endswith(".robinhood.com")
        ):
            raise ValueError(
                f"Robinhood OAuth {field} must use a Robinhood HTTPS endpoint"
            )
    return metadata

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

READ_ONLY_DATA_TOOLS = frozenset(
    {
        "get_earnings_calendar",
        "get_earnings_results",
        "get_equity_fundamentals",
        "get_equity_historicals",
        "get_equity_quotes",
        "get_equity_technical_indicators",
        "get_equity_tradability",
        "get_financials",
        "get_scans",
        "run_scan",
        "search",
    }
)

READ_ONLY_MCP_TOOLS = READ_ONLY_DATA_TOOLS | frozenset(
    {
        "get_option_chains",
        "get_option_instruments",
        "get_option_quotes",
    }
)


def _access_token_fingerprint(access_token: str | None) -> str | None:
    if not access_token:
        return None
    return hashlib.sha256(access_token.encode("utf-8")).hexdigest()


class CredentialStore:
    """Persist OAuth material in a current-user DPAPI encrypted local file.

    Robinhood access and refresh tokens can exceed the Windows Credential
    Manager secret-size limit. DPAPI encrypts the full envelope for the current
    Windows user while ``state/`` keeps the file outside version control.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = RLock()

    def _envelope_lock(self) -> InterProcessFileLock:
        return InterProcessFileLock(
            self.path.with_suffix(self.path.suffix + ".envelope.lock"),
            timeout_seconds=MAX_AUTH_RECONNECT_LOCK_TIMEOUT_SECONDS,
        )

    @contextmanager
    def _locked_envelope(self) -> Iterator[dict[str, Any]]:
        with self._lock:
            lock = self._envelope_lock()
            if not lock.acquire():
                raise TimeoutError("timed out waiting for Robinhood OAuth credential envelope lock")
            try:
                yield self._read()
            finally:
                lock.release()

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
            envelope = self._read()
            raw = envelope.get("tokens")
            legacy_modified_at = self.path.stat().st_mtime if self.path.exists() else None
        if not raw:
            return None
        tokens = OAuthToken.model_validate(raw)
        if tokens.expires_in is None:
            return tokens.model_copy(update={"expires_in": 0})
        expires_at = envelope.get("token_expires_at_epoch")
        if expires_at is None:
            saved_at = envelope.get("token_saved_at_epoch", legacy_modified_at)
            if saved_at is not None:
                expires_at = float(saved_at) + int(tokens.expires_in)
                # Persist the legacy absolute expiry before another metadata
                # update changes the DPAPI file mtime used as the fallback.
                with self._locked_envelope() as latest:
                    latest_raw = latest.get("tokens")
                    if latest_raw:
                        latest_tokens = OAuthToken.model_validate(latest_raw)
                        latest_expires_at = latest.get("token_expires_at_epoch")
                        if latest_expires_at is None:
                            latest_saved_at = latest.get(
                                "token_saved_at_epoch", saved_at
                            )
                            latest_expires_at = float(latest_saved_at) + int(
                                latest_tokens.expires_in or 0
                            )
                            latest["token_saved_at_epoch"] = float(latest_saved_at)
                            latest["token_expires_at_epoch"] = latest_expires_at
                            self._write(latest)
                        tokens = latest_tokens
                        expires_at = latest_expires_at
        if expires_at is None:
            return tokens
        remaining = max(
            0,
            math.ceil(float(expires_at) - time.time())
            - TOKEN_REFRESH_SKEW_SECONDS,
        )
        return tokens.model_copy(update={"expires_in": remaining})

    async def set_tokens(self, tokens: OAuthToken) -> None:
        with self._locked_envelope() as envelope:
            existing_raw = envelope.get("tokens")
            if not tokens.refresh_token and existing_raw:
                existing = OAuthToken.model_validate(existing_raw)
                if existing.refresh_token:
                    tokens = tokens.model_copy(
                        update={"refresh_token": existing.refresh_token}
                    )
            saved_at = time.time()
            envelope["tokens"] = tokens.model_dump(mode="json")
            envelope["token_saved_at_epoch"] = saved_at
            if tokens.expires_in is None:
                envelope.pop("token_expires_at_epoch", None)
            else:
                envelope["token_expires_at_epoch"] = saved_at + int(
                    tokens.expires_in
                )
            self._write(envelope)

    async def mark_access_token_expired(self, expected_access_token: str) -> bool:
        """Force a one-time refresh without discarding the saved refresh token."""
        with self._locked_envelope() as envelope:
            raw = envelope.get("tokens")
            if not raw:
                return False
            tokens = OAuthToken.model_validate(raw)
            if tokens.access_token != expected_access_token or not tokens.refresh_token:
                return False
            envelope["token_expires_at_epoch"] = time.time()
            self._write(envelope)
        return True

    async def auth_reconnect_cooldown_remaining(self) -> int:
        with self._lock:
            envelope = self._read()
            blocked_until = envelope.get("auth_reconnect_blocked_until_epoch")
            try:
                remaining = math.ceil(float(blocked_until) - time.time())
            except (TypeError, ValueError):
                return 0
            if remaining > 0:
                return remaining
            # An expired marker is harmless. Do not rewrite the shared token
            # envelope merely to remove it because another worker may be
            # persisting a rotated refresh token at the same time.
        return 0

    async def auth_reconnect_blocked_access_token_fingerprint(self) -> str | None:
        with self._lock:
            value = self._read().get("auth_reconnect_blocked_access_token_sha256")
        return str(value) if isinstance(value, str) and value else None

    async def set_auth_reconnect_cooldown(
        self,
        seconds: float,
        *,
        access_token_fingerprint: str | None = None,
    ) -> None:
        with self._locked_envelope() as envelope:
            envelope["auth_reconnect_blocked_until_epoch"] = time.time() + max(1.0, seconds)
            if access_token_fingerprint:
                envelope["auth_reconnect_blocked_access_token_sha256"] = access_token_fingerprint
            else:
                envelope.pop("auth_reconnect_blocked_access_token_sha256", None)
            self._write(envelope)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        with self._lock:
            raw = self._read().get("client_info")
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        with self._locked_envelope() as envelope:
            envelope["client_info"] = client_info.model_dump(mode="json")
            self._write(envelope)

    async def get_oauth_metadata(self) -> OAuthMetadata | None:
        with self._lock:
            raw = self._read().get("oauth_metadata")
        return OAuthMetadata.model_validate(raw) if raw else None

    async def set_oauth_metadata(self, metadata: OAuthMetadata) -> None:
        with self._locked_envelope() as envelope:
            envelope["oauth_metadata"] = metadata.model_dump(mode="json")
            self._write(envelope)

    def archive_existing(self) -> Path | None:
        """Keep an unreadable credential blob out of the active OAuth path."""
        with self._locked_envelope():
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


class PersistentOAuthClientProvider(OAuthClientProvider):
    """Restore persisted token expiry and retain non-rotated refresh tokens."""

    def __init__(
        self,
        *args: Any,
        bootstrap_oauth_metadata: OAuthMetadata,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.bootstrap_oauth_metadata = bootstrap_oauth_metadata

    async def _persist_oauth_metadata(self) -> None:
        metadata = self.context.oauth_metadata
        storage = self.context.storage
        if metadata is not None and isinstance(storage, CredentialStore):
            await storage.set_oauth_metadata(
                _validated_robinhood_oauth_metadata(metadata)
            )

    async def _initialize(self) -> None:
        await super()._initialize()
        storage = self.context.storage
        persisted_metadata = (
            await storage.get_oauth_metadata()
            if isinstance(storage, CredentialStore)
            else None
        )
        self.context.oauth_metadata = _validated_robinhood_oauth_metadata(
            persisted_metadata or self.bootstrap_oauth_metadata
        )
        if persisted_metadata is None and isinstance(storage, CredentialStore):
            await storage.set_oauth_metadata(self.bootstrap_oauth_metadata)
        if self.context.current_tokens is not None:
            self.context.update_token_expiry(self.context.current_tokens)

    async def _handle_token_response(self, response: httpx.Response) -> None:
        await super()._handle_token_response(response)
        await self._persist_oauth_metadata()

    async def _handle_refresh_response(self, response: httpx.Response) -> bool:
        prior_refresh_token = (
            self.context.current_tokens.refresh_token
            if self.context.current_tokens is not None
            else None
        )
        refreshed = await super()._handle_refresh_response(response)
        if (
            refreshed
            and prior_refresh_token
            and self.context.current_tokens is not None
            and not self.context.current_tokens.refresh_token
        ):
            tokens = self.context.current_tokens.model_copy(
                update={"refresh_token": prior_refresh_token}
            )
            self.context.current_tokens = tokens
            await self.context.storage.set_tokens(tokens)
        if refreshed:
            await self._persist_oauth_metadata()
        return refreshed


class ReadOnlyMcpSession:
    """Expose only safe MCP session operations to the paper runtime."""

    __slots__ = ("__list_tools", "__send_ping", "__call_tool")

    def __init__(self, session: ClientSession) -> None:
        self.__list_tools = lambda: session.list_tools()
        self.__send_ping = lambda: session.send_ping()
        self.__call_tool = (
            lambda tool_name, arguments: session.call_tool(
                tool_name,
                arguments=arguments,
            )
        )

    async def list_tools(self) -> Any:
        return await self.__list_tools()

    async def send_ping(self) -> Any:
        return await self.__send_ping()

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        if tool_name not in READ_ONLY_MCP_TOOLS:
            raise RuntimeError(
                f"Robinhood tool is outside the read-only data allowlist: {tool_name}"
            )
        return await self.__call_tool(tool_name, arguments)


class RobinhoodMcpCapabilityClient:
    """Create an OAuth session and audit only the server's declared tools."""

    def __init__(
        self,
        config: dict[str, Any],
        root: str | Path | None = None,
        *,
        interactive_oauth: bool = True,
    ) -> None:
        self.endpoint = str(config.get("endpoint", ROBINHOOD_TRADING_MCP_URL))
        base = Path(root).resolve() if root is not None else PROJECT_ROOT
        configured_path = Path(str(config.get("credential_store_path", "state/robinhood_mcp_oauth.dpapi")))
        self.store = CredentialStore(configured_path if configured_path.is_absolute() else base / configured_path)
        self.redirect_uri = str(config.get("redirect_uri", "http://127.0.0.1:8765/callback"))
        self.client_name = str(config.get("client_name", "auto-trading-skill read-only capability audit"))
        self.interactive_oauth = interactive_oauth
        self.request_timeout_seconds = float(config.get("request_timeout_seconds", 20))
        self.auth_reconnect_cooldown_seconds = float(
            config.get("auth_reconnect_cooldown_seconds", AUTH_RECONNECT_COOLDOWN_SECONDS)
        )
        self.auth_reconnect_lock_timeout_seconds = min(
            MAX_AUTH_RECONNECT_LOCK_TIMEOUT_SECONDS,
            max(0.1, self.request_timeout_seconds),
        )
        self.oauth_metadata = self._validated_oauth_metadata(
            dict(config.get("oauth_metadata", {}))
        )

    def _validated_oauth_metadata(self, raw: dict[str, Any]) -> OAuthMetadata:
        metadata = OAuthMetadata(
            issuer=raw.get("issuer", self.endpoint),
            authorization_endpoint=raw.get(
                "authorization_endpoint", "https://robinhood.com/oauth"
            ),
            token_endpoint=raw.get(
                "token_endpoint", "https://api.robinhood.com/oauth2/token/"
            ),
            registration_endpoint=raw.get(
                "registration_endpoint",
                "https://agent.robinhood.com/oauth/trading/register",
            ),
            scopes_supported=raw.get("scopes_supported", ["internal"]),
            grant_types_supported=raw.get(
                "grant_types_supported", ["authorization_code", "refresh_token"]
            ),
            token_endpoint_auth_methods_supported=raw.get(
                "token_endpoint_auth_methods_supported", ["none"]
            ),
            code_challenge_methods_supported=raw.get(
                "code_challenge_methods_supported", ["S256"]
            ),
        )
        return _validated_robinhood_oauth_metadata(metadata)

    async def _show_authorization_url(self, authorization_url: str) -> None:
        if not self.interactive_oauth:
            raise RuntimeError(
                "Robinhood OAuth authorization is required; run "
                "python -m scripts.broker.robinhood_mcp_audit --reset-credentials interactively"
            )
        print("Open this Robinhood OAuth URL in a desktop browser, approve access, then paste the final callback URL:")
        print(authorization_url)

    async def _read_callback(self) -> tuple[str, str | None]:
        if not self.interactive_oauth:
            raise RuntimeError("interactive Robinhood OAuth is disabled in the paper runtime")
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
        return PersistentOAuthClientProvider(
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
            bootstrap_oauth_metadata=self.oauth_metadata,
        )

    def _session_deadline_seconds(self) -> float | None:
        # Browser authorization is a human-paced, one-off CLI operation. Its
        # network requests remain bounded by httpx, but the time spent waiting
        # for the user must not consume the paper runtime's hard deadline.
        return None if self.interactive_oauth else self.request_timeout_seconds

    def _refresh_lock(self) -> InterProcessFileLock:
        return InterProcessFileLock(
            self.store.path.with_suffix(self.store.path.suffix + ".refresh.lock"),
            timeout_seconds=self.auth_reconnect_lock_timeout_seconds,
        )

    async def _acquire_lock(self, lock: InterProcessFileLock) -> bool:
        acquisition = asyncio.create_task(asyncio.to_thread(lock.acquire))
        try:
            return await asyncio.shield(acquisition)
        except asyncio.CancelledError:
            def release_if_acquired(future: asyncio.Future[bool]) -> None:
                try:
                    if future.result():
                        lock.release()
                except BaseException:
                    return

            acquisition.add_done_callback(release_if_acquired)
            raise

    async def _has_valid_access_token(
        self,
        *,
        different_from_fingerprint: str | None = None,
    ) -> bool:
        tokens = await self.store.get_tokens()
        if (
            tokens is None
            or tokens.expires_in is None
            or tokens.expires_in <= 0
        ):
            return False
        if different_from_fingerprint is None:
            return True
        return _access_token_fingerprint(tokens.access_token) != different_from_fingerprint

    def _readonly_operation_timeout_seconds(self) -> float | None:
        if self.interactive_oauth:
            return None
        return (
            self.request_timeout_seconds * 2
            + self.auth_reconnect_lock_timeout_seconds * 2
            + 1.0
        )

    async def _acquire_refresh_lock_if_needed(
        self,
    ) -> InterProcessFileLock | None:
        tokens = await self.store.get_tokens()
        if (
            tokens is None
            or not tokens.refresh_token
            or tokens.expires_in is None
            or tokens.expires_in > 0
        ):
            return None
        lock = self._refresh_lock()
        if not await self._acquire_lock(lock):
            if await self._has_valid_access_token():
                return None
            raise TimeoutError("timed out waiting for Robinhood OAuth refresh lock")
        refreshed_by_peer = await self.store.get_tokens()
        if (
            refreshed_by_peer is not None
            and refreshed_by_peer.expires_in is not None
            and refreshed_by_peer.expires_in > 0
        ):
            lock.release()
            return None
        return lock

    @classmethod
    def _is_recoverable_auth_failure(cls, exc: BaseException) -> bool:
        if isinstance(exc, BaseExceptionGroup):
            return any(
                cls._is_recoverable_auth_failure(child)
                for child in exc.exceptions
            )
        response = getattr(exc, "response", None)
        if getattr(response, "status_code", None) == 401:
            return True
        message = str(exc).lower()
        return any(
            marker in message
            for marker in (
                "401 unauthorized",
                "status code 401",
                "robinhood oauth authorization is required",
                "interactive robinhood oauth is disabled",
            )
        )

    @classmethod
    def _contains_cancellation(cls, exc: BaseException) -> bool:
        if isinstance(exc, asyncio.CancelledError):
            return True
        if isinstance(exc, BaseExceptionGroup):
            return any(cls._contains_cancellation(child) for child in exc.exceptions)
        return False

    async def _force_refresh_after_auth_failure(
        self,
        rejected_access_token_fingerprint: str | None = None,
    ) -> bool:
        """Expire the rejected access token so the next session uses refresh_token."""
        if self.interactive_oauth:
            return False
        if rejected_access_token_fingerprint and await self._has_valid_access_token(
            different_from_fingerprint=rejected_access_token_fingerprint
        ):
            return True
        tokens = await self.store.get_tokens()
        if tokens is None or not tokens.refresh_token:
            return False
        lock = self._refresh_lock()
        if not await self._acquire_lock(lock):
            return await self._has_valid_access_token(
                different_from_fingerprint=rejected_access_token_fingerprint
            )
        try:
            latest = await self.store.get_tokens()
            if latest is None or not latest.refresh_token:
                return False
            if (
                rejected_access_token_fingerprint
                and _access_token_fingerprint(latest.access_token)
                != rejected_access_token_fingerprint
                and latest.expires_in is not None
                and latest.expires_in > 0
            ):
                return True
            return await self.store.mark_access_token_expired(latest.access_token)
        finally:
            lock.release()

    async def _run_readonly_operation(
        self,
        operation: Callable[[ReadOnlyMcpSession], Awaitable[T]],
    ) -> T:
        """Retry one read-only operation after a rejected but refreshable token."""
        async with asyncio.timeout(self._readonly_operation_timeout_seconds()):
            if not self.interactive_oauth:
                remaining = await self.store.auth_reconnect_cooldown_remaining()
                if remaining > 0:
                    blocked_fingerprint = (
                        await self.store.auth_reconnect_blocked_access_token_fingerprint()
                    )
                    if not (
                        blocked_fingerprint
                        and await self._has_valid_access_token(
                            different_from_fingerprint=blocked_fingerprint
                        )
                    ):
                        raise RuntimeError(
                            "Robinhood OAuth reconnect is temporarily blocked after a failed refresh; "
                            f"retry in {remaining} seconds or reauthorize credentials interactively"
                        )

            forced_refresh = False
            for _attempt in range(2):
                tokens = await self.store.get_tokens()
                rejected_access_token_fingerprint = _access_token_fingerprint(
                    tokens.access_token if tokens is not None else None
                )
                try:
                    async with self._session() as session:
                        result = await operation(session)
                except BaseException as exc:
                    if self._contains_cancellation(exc):
                        raise
                    if self.interactive_oauth or not self._is_recoverable_auth_failure(exc):
                        raise
                    if not forced_refresh and await self._force_refresh_after_auth_failure(
                        rejected_access_token_fingerprint
                    ):
                        forced_refresh = True
                        continue
                    await self.store.set_auth_reconnect_cooldown(
                        self.auth_reconnect_cooldown_seconds,
                        access_token_fingerprint=rejected_access_token_fingerprint,
                    )
                    raise RuntimeError(
                        "Robinhood OAuth reconnect failed after one controlled refresh; "
                        "run python -m scripts.broker.robinhood_mcp_audit --reset-credentials interactively"
                    ) from exc
                return result
            raise RuntimeError("Robinhood OAuth reconnect retry loop exhausted")

    async def _call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        if tool_name not in READ_ONLY_MCP_TOOLS:
            raise RuntimeError(
                f"Robinhood tool is outside the read-only data allowlist: {tool_name}"
            )
        return await self._run_readonly_operation(
            lambda session: session.call_tool(tool_name, arguments=arguments)
        )

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[ReadOnlyMcpSession]:
        oauth_logger = logging.getLogger("mcp.client.auth.oauth2")
        previous_disabled = oauth_logger.disabled
        refresh_lock: InterProcessFileLock | None = None
        if not self.interactive_oauth:
            oauth_logger.disabled = True
        try:
            async with asyncio.timeout(self._session_deadline_seconds()):
                refresh_lock = await self._acquire_refresh_lock_if_needed()
                oauth = self._oauth()
                async with httpx.AsyncClient(
                    auth=oauth,
                    follow_redirects=True,
                    timeout=self.request_timeout_seconds,
                ) as http_client:
                    # Robinhood accepts the MCP Streamable HTTP session but
                    # returns HTTP 400 to the SDK's optional DELETE termination
                    # request. Let the server expire the read-only session.
                    async with streamable_http_client(
                        self.endpoint,
                        http_client=http_client,
                        terminate_on_close=False,
                    ) as (read_stream, write_stream, _):
                        async with ClientSession(read_stream, write_stream) as session:
                            await session.initialize()
                            if refresh_lock is not None:
                                refresh_lock.release()
                                refresh_lock = None
                            yield ReadOnlyMcpSession(session)
        finally:
            if refresh_lock is not None:
                refresh_lock.release()
            oauth_logger.disabled = previous_disabled

    async def probe(self) -> dict[str, Any]:
        """Validate the persisted OAuth session without invoking a broker tool."""
        result = await self._run_readonly_operation(
            lambda session: session.list_tools()
        )
        names = {tool.name for tool in result.tools}
        return {
            "authenticated": True,
            "tool_count": len(names),
            "tool_names": sorted(names),
            "readonly_quote_tool_available": "get_equity_quotes" in names,
        }

    async def audit(self) -> CapabilityAudit:
        async def ping_and_list_tools(session: ReadOnlyMcpSession) -> Any:
            await session.send_ping()
            return await session.list_tools()

        result = await self._run_readonly_operation(ping_and_list_tools)
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
        result = await self._call_tool("get_equity_quotes", {"symbols": normalized})
        payload = result.structuredContent
        if not isinstance(payload, dict):
            raise RuntimeError("Robinhood get_equity_quotes returned no structured payload")
        return payload

    async def get_equity_historicals(
        self,
        symbols: list[str],
        start_time: str,
        end_time: str,
        *,
        interval: str = "5minute",
    ) -> dict[str, Any]:
        normalized = sorted({symbol.strip().upper() for symbol in symbols if symbol.strip()})
        if not normalized or len(normalized) > 10:
            raise ValueError("Robinhood get_equity_historicals requires 1 to 10 symbols")
        arguments = {
            "symbols": normalized,
            "start_time": start_time,
            "end_time": end_time,
            "interval": interval,
            "bounds": "regular",
            "adjustment_type": "split",
        }
        result = await self._call_tool("get_equity_historicals", arguments)
        return self._structured_payload(result.structuredContent, "get_equity_historicals")

    async def get_scans(self) -> dict[str, Any]:
        return await self._call_readonly("get_scans", {})

    async def run_scan(self, scan_id: str) -> dict[str, Any]:
        scan_id = scan_id.strip()
        if not scan_id:
            raise ValueError("scan_id is required")
        return await self._call_readonly("run_scan", {"scan_id": scan_id})

    async def search_instruments(self, query: str, limit: int = 5) -> dict[str, Any]:
        query = query.strip()
        if not query:
            raise ValueError("instrument search query is required")
        return await self._call_readonly(
            "search",
            {"query": query, "asset_type": "instrument", "limit": max(1, min(int(limit), 20))},
        )

    async def get_equity_fundamentals(self, symbols: list[str]) -> dict[str, Any]:
        normalized = self._normalize_symbols(symbols, maximum=10, tool_name="get_equity_fundamentals")
        return await self._call_readonly("get_equity_fundamentals", {"symbols": normalized, "bounds": "regular"})

    async def get_financials(self, symbols: list[str], *, period: str = "quarterly", limit: int = 4) -> dict[str, Any]:
        normalized = self._normalize_symbols(symbols, maximum=20, tool_name="get_financials")
        if period not in {"quarterly", "annual"}:
            raise ValueError("financial period must be quarterly or annual")
        return await self._call_readonly(
            "get_financials",
            {"symbols": normalized, "period": period, "limit": max(1, min(int(limit), 40))},
        )

    async def get_equity_technical_indicators(
        self,
        symbol: str,
        indicator_type: str,
        interval: str,
        start_time: str,
        end_time: str,
        *,
        output: str = "latest",
        period: int | None = None,
    ) -> dict[str, Any]:
        normalized = self._normalize_symbols([symbol], maximum=1, tool_name="get_equity_technical_indicators")[0]
        arguments: dict[str, Any] = {
            "symbol": normalized,
            "type": indicator_type,
            "interval": interval,
            "start_time": start_time,
            "end_time": end_time,
            "bounds": "regular",
            "adjustment_type": "split",
            "output": output,
        }
        if period is not None:
            arguments["period"] = int(period)
        return await self._call_readonly("get_equity_technical_indicators", arguments)

    async def get_earnings_results(self, symbol: str) -> dict[str, Any]:
        normalized = self._normalize_symbols([symbol], maximum=1, tool_name="get_earnings_results")[0]
        return await self._call_readonly("get_earnings_results", {"symbol": normalized})

    async def get_equity_tradability(self, account_number: str, symbols: list[str]) -> dict[str, Any]:
        """Read tradability only when the caller explicitly supplies an account.

        Discovery does not call this method because a local paper strategy must
        not infer or persist a live account number.
        """
        account_number = account_number.strip()
        if not account_number:
            raise ValueError("account_number must be explicitly supplied")
        normalized = self._normalize_symbols(symbols, maximum=10, tool_name="get_equity_tradability")
        return await self._call_readonly(
            "get_equity_tradability",
            {"account_number": account_number, "symbols": normalized},
        )

    async def get_option_chains(self, underlying_symbol: str) -> dict[str, Any]:
        symbol = underlying_symbol.strip().upper()
        if not symbol:
            raise ValueError("underlying_symbol is required")
        result = await self._call_tool("get_option_chains", {"underlying_symbol": symbol})
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
        result = await self._call_tool("get_option_instruments", arguments)
        return self._structured_payload(result.structuredContent, "get_option_instruments")

    async def get_option_quotes(self, option_ids: list[str]) -> dict[str, Any]:
        normalized = list(dict.fromkeys(value.strip() for value in option_ids if value.strip()))
        if not normalized:
            return {"data": {"results": []}}
        if len(normalized) > 20:
            raise ValueError("Robinhood option quote batches are limited to 20 contracts")
        result = await self._call_tool("get_option_quotes", {"instrument_ids": normalized})
        return self._structured_payload(result.structuredContent, "get_option_quotes")

    async def get_earnings_calendar(self, start_date: str, days: int = 7) -> dict[str, Any]:
        if days == 0 or not -31 <= days <= 31:
            raise ValueError("earnings calendar days must be between -31 and 31 and non-zero")
        result = await self._call_tool(
            "get_earnings_calendar", {"start_date": start_date, "days": days}
        )
        return self._structured_payload(result.structuredContent, "get_earnings_calendar")

    async def get_high_market_cap_earnings_calendar(self, start_date: str, days: int = 7) -> dict[str, Any]:
        if days == 0 or not -31 <= days <= 31:
            raise ValueError("earnings calendar days must be between -31 and 31 and non-zero")
        return await self._call_readonly(
            "get_earnings_calendar",
            {"start_date": start_date, "days": days, "filter": "high_market_cap"},
        )

    async def _call_readonly(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if tool_name not in READ_ONLY_DATA_TOOLS:
            raise RuntimeError(f"Robinhood tool is outside the read-only data allowlist: {tool_name}")
        result = await self._call_tool(tool_name, arguments)
        return self._structured_payload(result.structuredContent, tool_name)

    @staticmethod
    def _normalize_symbols(symbols: list[str], *, maximum: int, tool_name: str) -> list[str]:
        normalized = sorted({symbol.strip().upper() for symbol in symbols if symbol.strip()})
        if not normalized or len(normalized) > maximum:
            raise ValueError(f"Robinhood {tool_name} requires 1 to {maximum} symbols")
        return normalized

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
