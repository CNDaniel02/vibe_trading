---
name: auto-trading-skill
description: Build, run, test, and review an equity plus long-call/long-put paper/shadow trading loop with deterministic screening, provider-neutral LLM research agents, strict schemas, shared-account risk gates, simulated fills, virtual state, historical replay, and forward paper evaluation. Do not use for live order placement.
---

# Auto Trading Skill

## Operating Boundary

Default to `paper` mode. Treat `live_trading` as disabled unless a human explicitly changes configuration and asks for a separate live-trading implementation. In paper mode, never call live order tools such as `place_equity_order`, `place_option_order`, or cancellation tools.

Use real market data only as observations. Route equity orders through `scripts/simulation/paper_broker.py` and long-premium option orders through `scripts/options/paper_broker.py`. Equity and options share cash within each virtual account. The AI-gated strategy uses a separate namespaced account so its statistics cannot contaminate the active deterministic account.

## Workflow

1. Load `config/paper_mode.yaml`, `paper_risk_limits.yaml`, `equity_universe.yaml`, and `execution_costs.yaml`.
2. Collect read-only Robinhood MCP or Alpaca bid/ask snapshots and Vibe OHLCV. Reject missing, stale, future-dated, or abnormal data.
3. Run deterministic validation plus weighted relative-strength scoring without model calls.
4. For baseline-screened candidates, run provider-neutral News, Challenge, and Decision agents with strict JSON Schema outputs.
5. Independently run `exa_deepseek_catalyst_v1`: read-only market discovery, Exa evidence, low-cost ranking, thinking Bull/News, Challenge, Decision, and deterministic risk veto.
6. Keep `relative_strength_v1` and `long_directional_options_v1` unchanged as deterministic shadow baselines.
7. Run `ai_gated_technical_v1` only against a bounded technical top set. Exa and DeepSeek may propose a trade, but execution is restricted to the isolated paper sleeve and the deterministic risk veto.
8. Independently run `llm_news_drift_v1`: market-wide Exa discovery, one price-blind headline classification, then deterministic ticker/tradability checks. It is shadow-only and has no broker.
9. Run deterministic risk checks after model synthesis; risk retains final veto authority.
10. Let the fill model decide `open`, `filled`, `rejected`, `expired`, or `cancelled`; never fill through a limit.
11. Persist account, positions, orders, counters, decisions, fills, model usage, evidence snapshots, outcomes, and audit events.
12. Monitor all paper sleeves and evaluate exits, including an independent EOD/overnight-recovery guard.
13. Compare active, baseline-shadow, AI-sleeve, and news-drift results before any strategy promotion.
14. Require the forward-evaluation thresholds in `config/evaluation.yaml`; do not promote from replay or backtest evidence alone.
15. For options, permit only buy-to-open long calls/puts and sell-to-close. Reject sell-to-open, short contracts, spreads, margin, 0DTE, exercise, and assignment.

## Key Scripts

- `scripts/orchestrator/run_paper_cycle.py`: legacy fixture-oriented single-cycle helper; it is not the production continuous entrypoint.
- `scripts/agents/investment_team.py`: preserved deterministic v1 audit baseline.
- `scripts/agents/api_investment_team.py`: v2 gated API-driven shadow pipeline.
- `scripts/agents/catalyst_investment_team.py`: independent catalyst extraction, ranking, Bull/News, Challenge, and Decision stages.
- `scripts/agents/ai_gated_investment_team.py`: bounded executable-paper ranking, evidence, challenge, and decision stages.
- `scripts/discovery/`: immutable evidence snapshots, event/ticker cooldowns, and independent discovery orchestration.
- `scripts/llm/`: provider abstraction, strict schemas, prompts, and usage tracking.
- `scripts/orchestrator/run_shadow_cycle.py`: one v2 shadow decision; never submits an order.
- `scripts/evaluation/evaluate_agents.py`: fixed-snapshot agent eval and strategy comparison.
- `scripts/replay/replay_run_manager.py`: historical replay using the same strategy, risk, broker, fill, exit, and journal path.
- `scripts/simulation/paper_broker.py`: paper order lifecycle and state persistence.
- `scripts/simulation/fill_model.py`: bid/ask, limit, spread, and slippage fill rules.
- `scripts/risk/risk_gate.py`: fail-closed pre-trade checks.
- `scripts/risk/shared_portfolio_risk.py`: account-wide equity/options deployment and daily-entry caps.
- `scripts/options/`: contract models, real bid/ask fill simulation, long-premium broker, selection, Greeks reference, and expiration/sellout exits.
- `scripts/runtime/scheduler.py`: APScheduler wrapper with lock and heartbeat guards.
- `scripts/runtime/watchdog.py`: heartbeat freshness and fail-closed runtime decision.
- `scripts/runtime/subprocess_runner.py`: hard worker deadlines and process-tree cleanup.
- `scripts/orchestrator/forward_paper_service.py`: NYSE-calendar-aware one-shot or continuous forward service.
- `scripts/orchestrator/dry_run_forward_pipeline.py`: isolated no-network end-to-end validation.
- `scripts/adapters/`: pinned Vibe, Robinhood/Alpaca quote, and Exa news boundaries.
- `scripts/replay/vibe_replay_run_manager.py`: Vibe 5-minute point-in-time replay using the shared paper kernel.
- `scripts/broker/robinhood_readonly_adapter.py`: read-only Robinhood adapter; no live write methods.
- `scripts/evaluation/calculate_metrics.py`: paper performance metrics.
- `scripts/evaluation/outcome_labeler.py`: point-in-time candidate maturation and adaptive-weight labels.
- `scripts/evaluation/generate_performance_report.py`: Markdown performance report.
- `scripts/news_drift/`: isolated SQLite event ledger and news-first shadow pipeline.
- `scripts/agents/news_drift_headline_agent.py`: one fast price-blind structured classifier.
- `scripts/evaluation/evaluate_news_drift.py`: event, firm-day, portfolio-day, and cost-sensitivity report.

## Maintenance Record

After any code, configuration, data-source, risk, evaluation, or runtime behavior change, prepend a dated entry to `DEVELOPMENT_LOG.md`. Include the evidence that motivated the change, affected files, safety impact, verification results, and whether the user-operated service must restart. Keep `PROJECT_ARCHITECTURE.md` aligned with the implemented pipeline and list incomplete capabilities explicitly.

## Required Safety Checks

Keep these invariants true when editing:

- Paper cash is separate from Robinhood cash.
- A created order is never treated as a position until filled.
- Buy fills use ask/limit, never midpoint.
- Sell fills use bid/limit, never midpoint.
- Slippage is always adverse to the agent.
- Missing, stale, future-dated, or abnormal market data rejects the decision.
- Long-only US equities and ordinary non-levered ETFs; options may express downside only through fully paid long puts.
- No all-in orders; max position size defaults to 25% of virtual equity.
- Daily trade count and duplicate/idempotency guards are enforced before fill.
- Audit logs are append-only JSONL records.
- LLMs cannot create orders, alter risk configuration, or access broker tools. Catalyst ticker extraction is permitted only inside its bounded discovery lane and every output must resolve to an exact eligible US-listed instrument before analysis.
- API keys are read only from the configured environment-variable name; Robinhood OAuth material is read only from the current-user DPAPI-encrypted file under `state/`.

## References

Read only what is needed:

- `references/paper_trading_policy.md` for mode boundaries.
- `references/risk_policy.md` for hard risk limits.
- `references/simulated_execution_policy.md` for fill rules.
- `references/evaluation_policy.md` for paper-vs-forward interpretation.
- `references/multi_agent_workflow.md` for deterministic gates and API-agent boundaries.
- `references/data_sources.md` for plugin/data-source decisions.
- `references/reused_components.md` for upstream projects and reuse notes.
- `references/vibe_integration.md` for the exact Vibe isolation boundary.
- `references/catalyst_strategy_policy.md` for discovery limits, evidence deduplication, cooldowns, and promotion boundaries.
- `references/weighted_and_ai_gated_strategy.md` for weighted scoring, adaptive labels, company-specific puts, and AI paper-sleeve isolation.
- `references/llm_news_drift_policy.md` for the news-first shadow lane, paper-replication boundary, and isolated P2 experiments.
