# Vibe-Trading Integration Boundary

Pinned checkout: `../references/repos/Vibe-Trading`

Pinned commit: `6fc038d37f1767ae429bab435654b9b425ae66f4`

## Reused Through Adapters

- Yahoo and fallback OHLCV loaders.
- Independent global-equity backtest engine and artifacts.
- Optional read-only Swarm runtime using this project's sanitized preset.
- Source normalization and OHLC validation behavior.

## Never Delegated To Vibe

- Virtual account balances or positions.
- Simulated order lifecycle and fills.
- Deterministic risk veto.
- Forward scheduler and NYSE session gate.
- Exit decisions, journals, promotion decisions, or live orders.

## Isolation

`scripts/adapters/vibe_bridge.py` is the only import boundary. It runs in a subprocess, accepts four explicit actions, validates paths and Swarm tools, and returns JSON. The Vibe checkout must be clean and match the pinned commit.

The allowed actions are:

- `fetch_bars`
- `run_backtest`
- `inspect_swarm`
- `run_swarm`

The research preset rejects tools containing order, trade, broker, account, position, cancel, shell, bash, or write-file capabilities. Research reports are saved as evidence-only artifacts and are not parsed into orders.
