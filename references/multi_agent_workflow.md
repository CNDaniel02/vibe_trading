# Versioned Multi-Agent Paper Trading Workflow

This project reuses ai-berkshire workflow ideas as policy, not as a live-trading engine.

## Strategy Isolation

- `relative_strength_v1` remains the active paper strategy and may reach the existing paper broker.
- `multi_agent_relative_strength_v2_candidate` is shadow-only and cannot create orders.
- The strategies receive the same immutable snapshot but do not share account or order state.

## v2 Pipeline

1. Run Regime Agent and Technical / Relative Strength Agent in deterministic Python.
2. Stop with zero model calls when session, quote, spread, liquidity, regime, or technical gates fail.
3. Call News Agent only for a screened ticker and require timestamped, source-grounded structured output.
4. Stop after News Agent when no grounded event is available.
5. Call Challenge Agent with the same snapshot plus prior outputs and preserve its explicit veto.
6. Call Decision Manager for `buy`, `hold`, `exit`, or `no_trade`.
7. Enforce ticker immutability and Challenge veto in Python.
8. Run the existing deterministic risk gate last. It can veto a model proposal and cannot be bypassed.

## Provider Boundary

- Agent business logic depends only on `LLMProvider`.
- `mock` is deterministic and used for tests; `api` is OpenAI-compatible; `local` is interface-only.
- API keys come only from environment variables and are never serialized.
- Record model, prompt version, token counts, latency, estimated cost, errors, and retries for each call.

## Evidence Rules

- Every input carries snapshot id, decision/cutoff timestamps, ticker, session, market data, technical signals, available news, and source metadata.
- Preserve exact event source, publication time, and first-seen time.
- Missing news, weak sources, stale evidence, or absent metadata are data gaps, not positive evidence.
- Reject quote or news timestamps beyond the data cutoff before any model call.
- A model cannot create orders, alter risk limits, expand the ticker universe, or call Robinhood tools.

## LangGraph Decision

Keep the first API version as a deterministic Python orchestrator. Add LangGraph only when durable checkpoints, conditional retries, human approval, or long-running graph state is required.
