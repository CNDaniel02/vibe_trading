from __future__ import annotations

import asyncio

from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from scripts.broker.robinhood_mcp_audit import CapabilityAudit, CredentialStore, EXPECTED_ROBINHOOD_TOOLS


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
