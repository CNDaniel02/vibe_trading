from __future__ import annotations

import asyncio
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

import httpx
import pytest
from mcp.shared.auth import OAuthClientInformationFull, OAuthMetadata, OAuthToken

import scripts.broker.robinhood_mcp_audit as robinhood_mcp_audit
from scripts.broker.robinhood_mcp_audit import (
    CapabilityAudit,
    CredentialStore,
    EXPECTED_ROBINHOOD_TOOLS,
    ReadOnlyMcpSession,
    RobinhoodMcpCapabilityClient,
)


def test_expected_robinhood_tool_manifest_has_fifty_tools():
    assert len(EXPECTED_ROBINHOOD_TOOLS) == 50
    assert {"get_accounts", "get_equity_quotes", "get_equity_historicals", "place_equity_order"} <= EXPECTED_ROBINHOOD_TOOLS


def test_capability_audit_rejects_missing_expected_tools():
    audit = CapabilityAudit("https://example.invalid/mcp", 49, ["get_equity_quotes"], [], [])
    assert not audit.passed


def test_credential_store_round_trips_as_dpapi_ciphertext(tmp_path):
    path = tmp_path / "oauth.dpapi"
    store = CredentialStore(path)

    async def run() -> None:
        await store.set_tokens(OAuthToken(access_token="token", refresh_token="refresh"))
        assert (await store.get_tokens()).access_token == "token"
        info = OAuthClientInformationFull(client_id="client", redirect_uris=["http://127.0.0.1/callback"])
        await store.set_client_info(info)
        assert (await store.get_client_info()).client_id == "client"

    asyncio.run(run())
    assert path.exists()
    assert b'"token"' not in path.read_bytes()


def test_credential_store_archives_existing_file(tmp_path):
    path = tmp_path / "oauth.dpapi"
    store = CredentialStore(path)
    store._write({"probe": True})

    archived = store.archive_existing()

    assert archived is not None
    assert archived.exists()
    assert not path.exists()
    assert store._read() == {}


def test_credential_store_returns_remaining_persisted_token_lifetime(tmp_path):
    path = tmp_path / "oauth.dpapi"
    store = CredentialStore(path)
    store._write(
        {
            "tokens": OAuthToken(
                access_token="token",
                refresh_token="refresh",
                expires_in=120,
            ).model_dump(mode="json"),
            "token_expires_at_epoch": time.time() + 120,
        }
    )

    tokens = asyncio.run(store.get_tokens())

    assert tokens is not None
    assert 0 < tokens.expires_in <= 60


def test_credential_store_refreshes_inside_expiry_skew(tmp_path):
    store = CredentialStore(tmp_path / "oauth.dpapi")
    store._write(
        {
            "tokens": OAuthToken(
                access_token="token",
                refresh_token="refresh",
                expires_in=120,
            ).model_dump(mode="json"),
            "token_expires_at_epoch": time.time() + 30,
        }
    )

    tokens = asyncio.run(store.get_tokens())

    assert tokens is not None
    assert tokens.expires_in == 0


def test_legacy_token_expiry_is_migrated_before_other_metadata_write(tmp_path):
    path = tmp_path / "oauth.dpapi"
    store = CredentialStore(path)
    store._write(
        {
            "tokens": OAuthToken(
                access_token="token",
                refresh_token="refresh",
                expires_in=180,
            ).model_dump(mode="json")
        }
    )
    legacy_saved_at = time.time() - 60
    os.utime(path, (legacy_saved_at, legacy_saved_at))

    first = asyncio.run(store.get_tokens())
    asyncio.run(
        store.set_oauth_metadata(
            OAuthMetadata(
                issuer="https://agent.robinhood.com/mcp/trading",
                authorization_endpoint="https://robinhood.com/oauth",
                token_endpoint="https://api.robinhood.com/oauth2/token/",
            )
        )
    )
    second = asyncio.run(store.get_tokens())

    assert first is not None and second is not None
    assert second.expires_in <= first.expires_in
    envelope = store._read()
    assert abs(envelope["token_saved_at_epoch"] - legacy_saved_at) < 0.01
    assert abs(envelope["token_expires_at_epoch"] - (legacy_saved_at + 180)) < 0.01


def test_credential_store_preserves_rotating_refresh_credential(tmp_path):
    store = CredentialStore(tmp_path / "oauth.dpapi")

    async def run() -> OAuthToken:
        await store.set_tokens(
            OAuthToken(
                access_token="first-access",
                refresh_token="refresh",
                expires_in=120,
            )
        )
        await store.set_tokens(OAuthToken(access_token="second-access", expires_in=120))
        return await store.get_tokens()

    tokens = asyncio.run(run())

    assert tokens.refresh_token == "refresh"


def test_credential_store_round_trips_oauth_metadata(tmp_path):
    store = CredentialStore(tmp_path / "oauth.dpapi")
    metadata = OAuthMetadata(
        issuer="https://agent.robinhood.com/mcp/trading",
        authorization_endpoint="https://robinhood.com/oauth",
        token_endpoint="https://api.robinhood.com/oauth2/token/",
    )

    async def run() -> OAuthMetadata:
        await store.set_oauth_metadata(metadata)
        return await store.get_oauth_metadata()

    restored = asyncio.run(run())

    assert str(restored.token_endpoint) == "https://api.robinhood.com/oauth2/token/"
    assert b"api.robinhood.com" not in store.path.read_bytes()


def test_persisted_oauth_provider_initializes_expiry_and_can_refresh(tmp_path):
    config = {"credential_store_path": "oauth.dpapi"}
    client = RobinhoodMcpCapabilityClient(
        config,
        root=tmp_path,
        interactive_oauth=False,
    )
    client.store._write(
        {
            "tokens": OAuthToken(
                access_token="expired-access",
                refresh_token="refresh",
                expires_in=120,
            ).model_dump(mode="json"),
            "token_expires_at_epoch": time.time() - 1,
            "client_info": OAuthClientInformationFull(
                client_id="client",
                redirect_uris=["http://127.0.0.1/callback"],
            ).model_dump(mode="json"),
        }
    )
    provider = client._oauth()

    asyncio.run(provider._initialize())

    assert provider.context.token_expiry_time is not None
    assert provider.context.is_token_valid() is False
    assert provider.context.can_refresh_token() is True
    refresh_request = asyncio.run(provider._refresh_token())
    assert str(refresh_request.url) == "https://api.robinhood.com/oauth2/token/"


def test_persisted_oauth_metadata_is_revalidated_before_refresh(tmp_path):
    client = RobinhoodMcpCapabilityClient(
        {"credential_store_path": "oauth.dpapi"},
        root=tmp_path,
        interactive_oauth=False,
    )
    client.store._write(
        {
            "oauth_metadata": OAuthMetadata(
                issuer="https://agent.robinhood.com/mcp/trading",
                authorization_endpoint="https://robinhood.com/oauth",
                token_endpoint="https://attacker.example/token",
            ).model_dump(mode="json"),
            "tokens": OAuthToken(
                access_token="expired-access",
                refresh_token="refresh",
                expires_in=120,
            ).model_dump(mode="json"),
            "token_expires_at_epoch": time.time() - 1,
        }
    )

    with pytest.raises(ValueError, match="Robinhood OAuth token_endpoint"):
        asyncio.run(client._oauth()._initialize())


def test_refresh_response_retains_refresh_token_and_persists_new_access(tmp_path):
    client = RobinhoodMcpCapabilityClient(
        {"credential_store_path": "oauth.dpapi"},
        root=tmp_path,
        interactive_oauth=False,
    )
    asyncio.run(
        client.store.set_tokens(
            OAuthToken(
                access_token="expired-access",
                refresh_token="stable-refresh",
                expires_in=0,
            )
        )
    )
    provider = client._oauth()
    asyncio.run(provider._initialize())
    response = httpx.Response(
        200,
        json={"access_token": "new-access", "token_type": "Bearer", "expires_in": 120},
        request=httpx.Request("POST", "https://api.robinhood.com/oauth2/token/"),
    )

    refreshed = asyncio.run(provider._handle_refresh_response(response))
    persisted = asyncio.run(client.store.get_tokens())

    assert refreshed is True
    assert provider.context.current_tokens.access_token == "new-access"
    assert provider.context.current_tokens.refresh_token == "stable-refresh"
    assert persisted is not None
    assert persisted.access_token == "new-access"
    assert persisted.refresh_token == "stable-refresh"


def test_expired_token_acquires_cross_process_refresh_lock(tmp_path):
    client = RobinhoodMcpCapabilityClient(
        {"credential_store_path": "oauth.dpapi"},
        root=tmp_path,
        interactive_oauth=False,
    )
    client.store._write(
        {
            "tokens": OAuthToken(
                access_token="expired-access",
                refresh_token="refresh",
                expires_in=120,
            ).model_dump(mode="json"),
            "token_expires_at_epoch": time.time() - 1,
        }
    )

    refresh_lock = asyncio.run(client._acquire_refresh_lock_if_needed())

    assert refresh_lock is not None
    assert refresh_lock.path.exists()
    refresh_lock.release()
    assert not refresh_lock.path.exists()


def test_second_worker_reloads_token_after_first_worker_refreshes(tmp_path):
    config = {"credential_store_path": "oauth.dpapi"}
    first = RobinhoodMcpCapabilityClient(config, root=tmp_path, interactive_oauth=False)
    second = RobinhoodMcpCapabilityClient(config, root=tmp_path, interactive_oauth=False)
    first.store._write(
        {
            "tokens": OAuthToken(
                access_token="expired-access",
                refresh_token="refresh",
                expires_in=120,
            ).model_dump(mode="json"),
            "token_expires_at_epoch": time.time() - 1,
        }
    )
    first_lock = asyncio.run(first._acquire_refresh_lock_if_needed())
    assert first_lock is not None

    with ThreadPoolExecutor(max_workers=1) as executor:
        waiting = executor.submit(
            lambda: asyncio.run(second._acquire_refresh_lock_if_needed())
        )
        time.sleep(0.05)
        asyncio.run(
            first.store.set_tokens(
                OAuthToken(
                    access_token="new-access",
                    refresh_token="new-refresh",
                    expires_in=120,
                )
            )
        )
        first_lock.release()
        second_lock = waiting.result(timeout=2)

    assert second_lock is None
    tokens = asyncio.run(second.store.get_tokens())
    assert tokens is not None
    assert tokens.access_token == "new-access"


def test_missing_expiry_with_refresh_token_is_treated_as_expired(tmp_path):
    store = CredentialStore(tmp_path / "oauth.dpapi")
    asyncio.run(
        store.set_tokens(
            OAuthToken(
                access_token="access",
                refresh_token="refresh",
            )
        )
    )

    tokens = asyncio.run(store.get_tokens())

    assert tokens is not None
    assert tokens.expires_in == 0
    assert tokens.refresh_token == "refresh"


def test_auth_rejection_forces_the_next_session_to_refresh(tmp_path):
    client = RobinhoodMcpCapabilityClient(
        {"credential_store_path": "oauth.dpapi"},
        root=tmp_path,
        interactive_oauth=False,
    )
    asyncio.run(
        client.store.set_tokens(
            OAuthToken(
                access_token="access",
                refresh_token="refresh",
                expires_in=120,
            )
        )
    )

    forced = asyncio.run(client._force_refresh_after_auth_failure())
    tokens = asyncio.run(client.store.get_tokens())

    assert forced is True
    assert tokens is not None
    assert tokens.access_token == "access"
    assert tokens.refresh_token == "refresh"
    assert tokens.expires_in == 0


def test_noninteractive_readonly_operation_retries_once_after_auth_rejection(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    client = RobinhoodMcpCapabilityClient(
        {"credential_store_path": "oauth.dpapi"},
        root=tmp_path,
        interactive_oauth=False,
    )
    attempts: list[str] = []
    forced: list[bool] = []

    @asynccontextmanager
    async def fake_session():
        yield object()

    async def operation(_session):
        attempts.append("attempt")
        if len(attempts) == 1:
            raise RuntimeError("Robinhood OAuth authorization is required")
        return "recovered"

    async def force_refresh(*_args: object) -> bool:
        forced.append(True)
        return True

    monkeypatch.setattr(client, "_session", fake_session)
    monkeypatch.setattr(client, "_force_refresh_after_auth_failure", force_refresh, raising=False)

    result = asyncio.run(client._run_readonly_operation(operation))

    assert result == "recovered"
    assert attempts == ["attempt", "attempt"]
    assert forced == [True]


def test_noninteractive_readonly_operation_unwraps_auth_exception_group(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    client = RobinhoodMcpCapabilityClient(
        {"credential_store_path": "oauth.dpapi"},
        root=tmp_path,
        interactive_oauth=False,
    )
    attempts: list[str] = []
    forced: list[bool] = []

    @asynccontextmanager
    async def fake_session():
        yield object()

    async def operation(_session):
        attempts.append("attempt")
        if len(attempts) == 1:
            raise ExceptionGroup("MCP task group", [RuntimeError("status code 401")])
        return "recovered"

    async def force_refresh(*_args: object) -> bool:
        forced.append(True)
        return True

    monkeypatch.setattr(client, "_session", fake_session)
    monkeypatch.setattr(client, "_force_refresh_after_auth_failure", force_refresh, raising=False)

    result = asyncio.run(client._run_readonly_operation(operation))

    assert result == "recovered"
    assert attempts == ["attempt", "attempt"]
    assert forced == [True]


def test_noninteractive_readonly_operation_preserves_cancellation_group(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    client = RobinhoodMcpCapabilityClient(
        {"credential_store_path": "oauth.dpapi"},
        root=tmp_path,
        interactive_oauth=False,
    )
    attempts: list[str] = []

    @asynccontextmanager
    async def fake_session():
        yield object()

    async def operation(_session):
        attempts.append("attempt")
        if len(attempts) == 1:
            raise BaseExceptionGroup(
                "MCP task group",
                [asyncio.CancelledError(), RuntimeError("status code 401")],
            )
        return "recovered"

    monkeypatch.setattr(client, "_session", fake_session)

    with pytest.raises(BaseExceptionGroup):
        asyncio.run(client._run_readonly_operation(operation))
    assert attempts == ["attempt"]


def test_private_tool_helper_rejects_live_order_tool(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    client = RobinhoodMcpCapabilityClient(
        {"credential_store_path": "oauth.dpapi"},
        root=tmp_path,
        interactive_oauth=False,
    )

    @asynccontextmanager
    async def fake_session():
        pytest.fail("live order tool must be rejected before a session is opened")
        yield object()

    monkeypatch.setattr(client, "_session", fake_session)

    with pytest.raises(RuntimeError, match="outside the read-only data allowlist"):
        asyncio.run(client._call_tool("place_equity_order", {}))


def test_readonly_session_facade_rejects_live_order_tool() -> None:
    class RawSession:
        async def call_tool(self, *_args, **_kwargs):
            pytest.fail("a live order tool reached the raw MCP session")

    session = ReadOnlyMcpSession(RawSession())

    with pytest.raises(RuntimeError, match="outside the read-only data allowlist"):
        asyncio.run(session.call_tool("cancel_equity_order", {}))
    assert not hasattr(session, "_session")


def test_client_does_not_expose_a_public_raw_mcp_session(tmp_path) -> None:
    client = RobinhoodMcpCapabilityClient(
        {"credential_store_path": "oauth.dpapi"},
        root=tmp_path,
        interactive_oauth=False,
    )

    assert "session" not in dir(client)


def test_cancelled_lock_acquisition_releases_late_acquired_lock(tmp_path):
    class DelayedLock:
        def __init__(self) -> None:
            self.started = threading.Event()
            self.allow_acquire = threading.Event()
            self.released = threading.Event()

        def acquire(self) -> bool:
            self.started.set()
            self.allow_acquire.wait(timeout=1)
            return True

        def release(self) -> None:
            self.released.set()

    client = RobinhoodMcpCapabilityClient(
        {"credential_store_path": "oauth.dpapi"},
        root=tmp_path,
        interactive_oauth=False,
    )
    lock = DelayedLock()

    async def run() -> None:
        task = asyncio.create_task(client._acquire_lock(lock))
        assert await asyncio.to_thread(lock.started.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        lock.allow_acquire.set()
        for _ in range(20):
            if lock.released.is_set():
                break
            await asyncio.sleep(0.01)
        assert lock.released.is_set()

    asyncio.run(run())


def test_noninteractive_readonly_operation_does_not_retry_non_auth_failure(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    client = RobinhoodMcpCapabilityClient(
        {"credential_store_path": "oauth.dpapi"},
        root=tmp_path,
        interactive_oauth=False,
    )
    attempts: list[str] = []

    @asynccontextmanager
    async def fake_session():
        yield object()

    async def operation(_session):
        attempts.append("attempt")
        raise RuntimeError("network reset")

    monkeypatch.setattr(client, "_session", fake_session)

    with pytest.raises(RuntimeError, match="network reset"):
        asyncio.run(client._run_readonly_operation(operation))

    assert attempts == ["attempt"]


def test_noninteractive_operation_fails_closed_during_auth_reconnect_cooldown(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    client = RobinhoodMcpCapabilityClient(
        {"credential_store_path": "oauth.dpapi"},
        root=tmp_path,
        interactive_oauth=False,
    )
    asyncio.run(client.store.set_auth_reconnect_cooldown(60))

    @asynccontextmanager
    async def fake_session():
        pytest.fail("cooldown must prevent another Robinhood MCP session")
        yield object()

    async def operation(_session):
        return "unexpected"

    monkeypatch.setattr(client, "_session", fake_session)

    with pytest.raises(RuntimeError, match="temporarily blocked"):
        asyncio.run(client._run_readonly_operation(operation))


def test_valid_token_from_peer_bypasses_expiring_auth_reconnect_cooldown(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    client = RobinhoodMcpCapabilityClient(
        {"credential_store_path": "oauth.dpapi"},
        root=tmp_path,
        interactive_oauth=False,
    )
    asyncio.run(
        client.store.set_tokens(
            OAuthToken(
                access_token="failed-access",
                refresh_token="refresh",
                expires_in=3600,
            )
        )
    )
    asyncio.run(
        client.store.set_auth_reconnect_cooldown(
            60,
            access_token_fingerprint=robinhood_mcp_audit._access_token_fingerprint(
                "failed-access"
            ),
        )
    )
    asyncio.run(
        client.store.set_tokens(
            OAuthToken(
                access_token="refreshed-access",
                refresh_token="refresh",
                expires_in=3600,
            )
        )
    )

    @asynccontextmanager
    async def fake_session():
        yield object()

    async def operation(_session):
        return "recovered-by-peer"

    monkeypatch.setattr(client, "_session", fake_session)

    assert asyncio.run(client._run_readonly_operation(operation)) == "recovered-by-peer"


def test_expired_auth_reconnect_cooldown_does_not_rewrite_credentials(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    store = CredentialStore(tmp_path / "oauth.dpapi")
    asyncio.run(
        store.set_tokens(
            OAuthToken(
                access_token="access",
                refresh_token="refresh",
                expires_in=3600,
            )
        )
    )
    asyncio.run(store.set_auth_reconnect_cooldown(1))
    real_time = time.time
    monkeypatch.setattr(robinhood_mcp_audit.time, "time", lambda: real_time() + 2)
    monkeypatch.setattr(
        store,
        "_write",
        lambda _envelope: pytest.fail("an expired cooldown must not rewrite the shared token envelope"),
    )

    assert asyncio.run(store.auth_reconnect_cooldown_remaining()) == 0


def test_missing_expiry_without_refresh_token_is_treated_as_expired(tmp_path):
    store = CredentialStore(tmp_path / "oauth.dpapi")
    asyncio.run(store.set_tokens(OAuthToken(access_token="access")))

    tokens = asyncio.run(store.get_tokens())

    assert tokens is not None
    assert tokens.expires_in == 0
