# Python Robinhood MCP connection

The project uses a Python MCP client for the read-only capability audit in
`scripts/broker/robinhood_mcp_audit.py` and, after that passes, a strict
read-only data allowlist. The allowlist includes quotes, historicals,
fundamentals, financials, technical indicators, earnings, saved scans,
instrument search, and option market data. It does not provide a public
generic tool caller and cannot place, cancel, review, or mutate broker orders.

## First authorization

Run from the project root:

```powershell
.\.venv\Scripts\python.exe -m scripts.broker.robinhood_mcp_audit
```

The script prints a Robinhood OAuth URL. Open it on a desktop browser, approve
the new **Python client**, and paste the final URL redirected to
`http://127.0.0.1:8765/callback` back into the terminal. The callback does not
need a server running; the authorization code is read from the pasted URL.

Firefox may show **Unable to connect** after the redirect. This is expected:
the script uses the URL only to receive the authorization code and deliberately
does not run a local callback server. Copy the full URL from Firefox's address
bar into the terminal prompt.

OAuth tokens and dynamic client registration metadata are saved to
`state/robinhood_mcp_oauth.dpapi`. The file is encrypted by Windows DPAPI for
the current Windows user, is ignored by Git, and is never written to
`.env.local`, `config/`, logs, or source control.

If a local encrypted credential later cannot be read, archive it and require a
fresh browser authorization instead of falling back to plaintext storage:

```powershell
.\.venv\Scripts\python.exe -m scripts.broker.robinhood_mcp_audit --reset-credentials
```

The Python client is distinct from Codex. Existing Codex authorization cannot
be copied into this service because OAuth client registrations and tokens are
client-scoped.

## Verification boundary

The audit performs only MCP `initialize`, `ping`, and `list_tools`. It expects
the currently verified 50-tool Robinhood Trading MCP manifest. Missing tools
make the command fail; newly exposed tools are reported for manual review.

This does not authorize account or mutation tools. Review, order,
cancellation, watchlist-write, scanner creation/update, and portfolio mutation
tools remain unavailable to the project. `get_equity_tradability` requires an
explicit live account number and is not called by catalyst discovery. A
separate live-trading milestone and narrow deny-by-default adapter remain
required.
