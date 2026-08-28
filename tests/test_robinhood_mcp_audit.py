from __future__ import annotations

import asyncio
import os
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from mcp.shared.auth import OAuthClientInformationFull, OAuthMetadata, OAuthToken

from scripts.broker.robinhood_mcp_audit import (
    CapabilityAudit,
    CredentialStore,
    EXPECTED_ROBINHOOD_TOOLS,
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
