# ADR-002: API-Orchestrated Research Is the Only Promotion Candidate

## Status

Accepted on 2026-07-20 for paper and shadow evaluation.  This decision does not enable live trading.

## Context

The project considered two ways to automate equity research and a future broker handoff:

1. A scheduled Codex CLI/TUI session that lets a general-purpose agent call research MCP tools and the Robinhood MCP directly.
2. A project-owned service that calls explicitly configured market-data, news, and LLM APIs through narrow adapters, then passes a typed proposal through project-owned risk and execution gates.

The repository already implements the safe foundation of option 2: immutable snapshots, deterministic screening, strict JSON-schema outputs, an independent challenge agent, a deterministic risk veto, a paper broker, replay, forward evaluation, append-only audit logs, an NYSE clock, and a process lock.  Vibe is deliberately isolated behind read-only adapters.

## Decision

Select option 2 as the only architecture eligible for promotion beyond shadow research.

- The local service owns schedules, retries, market-session checks, idempotency, risk limits, exits, audit records, performance evaluation, and any future broker boundary.
- LLMs receive an immutable evidence snapshot and may return only schema-validated research or a trade proposal.  They may not invoke broker tools, modify configuration, select an unbounded universe, or override risk.
- Codex CLI/TUI and Vibe may be used as on-demand or scheduled **read-only research sidecars**.  Their outputs must be normalized into the same snapshot/evidence contract before they can affect a shadow decision.
- The existing `relative_strength_v1` remains the active paper strategy.  The API-driven multi-agent strategy remains shadow-only until it passes the project's forward-evidence gate.
- Any future Robinhood integration must remain a narrow adapter after a separate, explicit live-trading milestone.  It must never expose arbitrary MCP tool access to an LLM or a general-purpose agent.

## Rationale

Codex/TUI orchestration is excellent for interactive research, code changes, and supervised experiments, but it is not a sufficiently deterministic production control plane.  MCP tool injection, conversational state, prompt drift, interactive authentication, and unattended session recovery are all unsuitable as sole controls for a trading system.

An API-orchestrated service has a stable interface surface, versioned prompts and schemas, replayable inputs, deterministic failure behavior, bounded credentials, and testable promotion gates.  This does not imply higher expected returns; neither architecture has evidence that establishes profitability.  It provides the stronger basis for measuring whether a strategy has an edge without conflating research quality with execution reliability.

## TradingAgents reuse boundary

`references/TradingAgents/` is a local, ignored reference clone.  It is not a runtime dependency and its virtual environment must remain separate from this project.

Adopt as design references:

- structured research, trader proposal, and portfolio-decision boundaries;
- explicit bull/bear or challenge reasoning with evidence carried forward;
- checkpoint/resume concepts for long-running research jobs;
- decision-memory concepts only after outcomes are tied to the same point-in-time data and exit rules.

Do not adopt directly:

- its full LangGraph/LangChain dependency graph;
- its Yahoo/social-data vendor chain as an execution-grade data source;
- unstructured debate text as an executable order instruction;
- its LLM-generated sizing, risk, or portfolio approval as a substitute for `scripts/risk/risk_gate.py`;
- its own backtest or simulated-exchange path.

## Consequences and next gates

Before adding any live-order adapter, complete all of the following:

1. Enable and validate the existing Alpaca and Exa forward sources.
2. Run the API strategy in shadow mode with fixed-snapshot agent evaluations.
3. Meet `config/evaluation.yaml` forward-session, closed-trade, return, profit-factor, drawdown, and zero-violation gates.
4. Add a separately tested broker adapter whose default is deny and whose review/placement steps require an explicit, non-LLM approval boundary.
5. Perform a distinct live-readiness and account-scope review; no paper result authorizes live trading.
