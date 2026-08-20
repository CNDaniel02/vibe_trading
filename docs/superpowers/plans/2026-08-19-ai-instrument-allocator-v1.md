# AI Instrument Allocator V1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a $10,000 isolated AI paper sleeve that emits uncalibrated signed-return buckets, deterministically compares equity and long options by conservative repricing, and manages horizon-aware positions without altering legacy ledgers.

**Architecture:** Preserve all legacy state and freeze only new entries for the two superseded executors. Reuse the existing read-only discovery/evidence and paper-broker boundaries, add a provider-neutral AI team plus deterministic signed-signal, repricing, allocator, risk, plan, and mandate components, then wire a two-speed scheduler and separate observability surface.

**Tech Stack:** Python 3.13, pytest, JSON Schema Draft 2020-12, PyYAML, APScheduler 3.x, exchange-calendars, existing OpenAI-compatible DeepSeek provider, existing Robinhood/Exa read-only adapters.

## Global Constraints

- `paper=true`, `live_readonly=false`, and `live_trading=false` remain mandatory.
- Existing $2,000 accounts, state, orders, fills, PnL, journals, and logs are never migrated or rewritten.
- `ai_instrument_allocator_v1` uses a new namespace and exactly `$10,000` initial cash.
- Raw model bucket probabilities are always `uncalibrated` and never produce probability EV.
- Only long equity, long call, and long put orders are executable.
- No model may create an order, edit risk configuration, call Robinhood write tools, or select a ticker outside the deterministic candidate set.
- `short_equity_counterfactual` is shadow-only and separate from long-put PnL.
- Every production behavior change follows red-green-refactor.

---

### Task 1: Freeze Legacy Entries and Support Explicit Sleeve Cash

**Files:**
- Modify: `config/strategy_profiles.yaml`
- Modify: `scripts/core/models.py`
- Modify: `scripts/options/models.py`
- Modify: `scripts/simulation/paper_broker.py`
- Modify: `scripts/options/paper_broker.py`
- Modify: `scripts/discovery/ai_gated_pipeline.py`
- Modify: `scripts/orchestrator/forward_paper_service.py`
- Test: `tests/test_ai_instrument_allocator.py`
- Test: `tests/test_weighted_ai_runtime.py`

**Interfaces:**
- `PaperBroker(root, config, *, namespace=None, initial_cash=None)`
- `OptionPaperBroker(root, config, *, namespace=None, initial_cash=None)`
- profile field `new_entries_enabled: false`

- [ ] Write tests proving the default profiles freeze new entries, old monitor/open-order processing remains callable, an existing account is not reset, and a fresh explicit namespace starts at $10,000.
- [ ] Run the focused tests and verify failures show missing freeze/cash behavior.
- [ ] Add explicit initial-cash constructor parameters without changing existing defaults; add optional strategy, planned-stop, and horizon metadata to order models with backward-compatible defaults.
- [ ] Add the entry-frozen checks before any new discovery/network/order path while leaving monitors and exits enabled.
- [ ] Run focused tests and commit `feat: isolate allocator cash and freeze legacy entries`.

### Task 2: Signed Return Signal Contract

**Files:**
- Create: `scripts/decision/signed_return_signal.py`
- Create: `schemas/ai_allocator_signal.schema.json`
- Modify: `scripts/llm/schemas.py`
- Modify: `scripts/llm/export_schemas.py`
- Create: `scripts/llm/prompts/ai_allocator_ranker.md`
- Create: `scripts/llm/prompts/ai_allocator_news_agent.md`
- Create: `scripts/llm/prompts/ai_allocator_challenge_agent.md`
- Create: `scripts/llm/prompts/ai_allocator_decision_manager.md`
- Create: `scripts/agents/ai_instrument_allocator_team.py`
- Modify: `scripts/llm/mock_provider.py`
- Modify: `config/llm.yaml`
- Test: `tests/test_ai_instrument_allocator.py`

**Interfaces:**
- `validate_signed_return_signal(signal: dict) -> None`
- `derive_signal_summary(signal: dict) -> dict`
- `AiInstrumentAllocatorTeam.rank(...) -> dict`
- `AiInstrumentAllocatorTeam.analyze(snapshot, ranking, *, stage) -> dict`

- [ ] Write failing tests for seven required buckets, exact sum-to-one tolerance, allowed horizons, mandatory `uncalibrated` status, Python-derived direction/magnitude, challenge veto, and no model-selected instrument.
- [ ] Run focused tests and verify schema/implementation failures.
- [ ] Add strict schemas, deterministic validation/derivation, prompts, provider-neutral team, and deterministic mock responses.
- [ ] Configure overnight Challenge/Decision agent names with thinking enabled and every fast-stage agent with thinking disabled.
- [ ] Export JSON schemas, rerun focused tests, and commit `feat: add signed return AI signal contract`.

### Task 3: Option Candidate Set and Scenario Repricing

**Files:**
- Modify: `config/options_universe.yaml`
- Modify: `scripts/adapters/robinhood_option_market_data_adapter.py`
- Create: `scripts/options/scenario_pricing.py`
- Test: `tests/test_ai_instrument_allocator.py`
- Test: `tests/test_weighted_ai_runtime.py`

**Interfaces:**
- `fetch_contract_candidates(..., min_dte, target_dte, max_dte, max_premium_usd) -> (list[tuple[OptionContract, OptionQuote]], dict)`
- `reprice_option_scenarios(contract, quote, *, spot, horizon_days, move_pct, iv_shifts, costs) -> dict`

- [ ] Write failing tests proving the adapter returns bounded candidates across DTEs, rejects spread above 2%, marks spread above 1.5% non-preferred, and repricing changes with spot, elapsed time, and IV.
- [ ] Run tests and confirm missing API/repricing failures.
- [ ] Add bounded multi-expiration retrieval while preserving existing best-contract APIs for legacy callers.
- [ ] Add midpoint-anchored Black-Scholes scenario repricing with conservative IV contraction, unchanged-IV, and expansion cases plus Delta/Gamma/Theta/Vega diagnostics.
- [ ] Run tests and commit `feat: add conservative option scenario repricing`.

### Task 4: Deterministic Instrument Allocator and Counterfactuals

**Files:**
- Create: `scripts/decision/instrument_allocator.py`
- Create: `schemas/instrument_allocation.schema.json`
- Create: `schemas/short_equity_counterfactual.schema.json`
- Modify: `config/strategy_profiles.yaml`
- Test: `tests/test_ai_instrument_allocator.py`

**Interfaces:**
- `allocate_instrument(signal, underlying_quote, option_candidates, account_state, config, now) -> dict`
- `build_same_instrument_counterfactual(allocation, *, nav_usd=2000) -> dict`
- `build_short_equity_counterfactual(signal, quote, costs) -> dict | None`

- [ ] Write failing tests for bullish equity/call comparison, bearish put-only execution, ambiguous no-trade, conservative hurdle rejection, null probability EV, exact same-instrument $2,000 affordability, and separate short benchmark.
- [ ] Run tests and verify missing allocator behavior.
- [ ] Implement deterministic scenario comparison, break-even calculations, quantity sizing, and explicit `probability_ev_available: false`.
- [ ] Implement the no-reselection counterfactual and shadow-only short benchmark.
- [ ] Run tests and commit `feat: add deterministic equity option allocator`.

### Task 5: Shared Risk and Underlying Exposure Controls

**Files:**
- Modify: `config/paper_risk_limits.yaml`
- Modify: `config/options_risk_limits.yaml`
- Modify: `config/shared_risk_limits.yaml`
- Modify: `scripts/risk/risk_gate.py`
- Modify: `scripts/options/risk_gate.py`
- Modify: `scripts/risk/shared_portfolio_risk.py`
- Test: `tests/test_ai_instrument_allocator.py`
- Test: `tests/test_options_paper.py`
- Test: `tests/test_paper_trading.py`

**Interfaces:**
- shared fields `max_total_open_positions: 3` and `one_exposure_per_underlying: true`
- equity fields `max_planned_loss_pct_of_equity: 0.01` and `require_planned_stop_for_strategies`
- option fields `max_order_risk_pct_of_equity: 0.03`, `max_line_deployed_pct_of_equity: 0.08`, and `max_open_positions: 3`

- [ ] Write failing tests for equity 25% notional, 1% planned-stop NAV loss, 3% option entry risk, 8% option aggregate risk, three total positions, one exposure per underlying, and same-session re-entry.
- [ ] Run tests and verify each intended rejection is absent.
- [ ] Add shared exposure counting and cross-line underlying checks to both final broker risk gates.
- [ ] Add planned-stop metadata validation for the new strategy only and update exact risk configuration.
- [ ] Run all risk tests and commit `feat: enforce allocator portfolio risk controls`.

### Task 6: Conditional Plans, Position Mandates, and New Pipeline

**Files:**
- Create: `scripts/strategies/allocator_state.py`
- Create: `scripts/exit/position_mandates.py`
- Create: `schemas/position_mandate.schema.json`
- Create: `scripts/discovery/ai_instrument_allocator_pipeline.py`
- Modify: `scripts/discovery/ai_gated_pipeline.py`
- Modify: `scripts/orchestrator/forward_paper_service.py`
- Modify: `config/integrations.yaml`
- Modify: `config/strategy_profiles.yaml`
- Test: `tests/test_ai_instrument_allocator.py`
- Test: `tests/test_weighted_ai_runtime.py`

**Interfaces:**
- `AllocatorStateStore.save_plan`, `active_plans`, `record_allocation`
- `PositionMandateStore.register_order`, `reconcile`, `for_exposure`, `close`
- `AiInstrumentAllocatorPipeline.run_stage(stage, now=None) -> dict`
- `AiInstrumentAllocatorPipeline.monitor_only(now=None, *, force_flatten=False) -> dict`

- [ ] Write failing tests that overnight/08:00/09:25 stages create no orders, 09:32 uses fresh quotes without LLM, intraday uses fast agents, and all state stays in the new namespace.
- [ ] Write failing restart tests for intraday, next-close, 2-5-day, missing-mandate, stop-loss, thesis-invalidation, and one-underlying exits.
- [ ] Run tests and confirm missing store/pipeline/exit behavior.
- [ ] Implement restart-safe plan and mandate stores with append-only events.
- [ ] Implement the new pipeline by reusing existing discovery/evidence methods, removing direction quotas, invoking the deterministic allocator, registering broker orders/mandates, and reconciling fills.
- [ ] Wire separate overnight, premarket update, pre-open revalidation, open execution, bounded intraday, monitor, and EOD jobs. Keep legacy monitors active even while entries are frozen.
- [ ] Run focused tests and commit `feat: run horizon aware allocator paper sleeve`.

### Task 7: Calibration-Safe Samples and Cost Accounting

**Files:**
- Create: `scripts/evaluation/probability_calibration.py`
- Create: `schemas/calibration_record.schema.json`
- Modify: `scripts/evaluation/calculate_metrics.py`
- Test: `tests/test_ai_instrument_allocator.py`

**Interfaces:**
- `build_expanding_walk_forward_splits(records, *, horizon, evaluation_time, minimum_train_size) -> list[dict]`
- `multiclass_brier_score(probabilities, actual_bucket) -> float`
- `multiclass_log_loss(probabilities, actual_bucket) -> float`
- metrics field `execution_cost_decomposition`

- [ ] Write failing tests that folds never include labels maturing after their cutoff, horizons never mix, Brier/log loss match exact fixtures, and raw signals expose no probability EV.
- [ ] Write failing closed-round-trip tests for midpoint PnL minus spread, slippage/tick, and commission equaling executable net PnL with near-zero residual.
- [ ] Implement maturity-safe split construction and scoring without fitting a calibrator.
- [ ] Add exact equity and option round-trip cost decomposition to metrics.
- [ ] Run focused tests and commit `feat: add calibration safe evaluation records`.

### Task 8: Dashboard, Architecture, and Development Log

**Files:**
- Modify: `scripts/dashboard/paper_dashboard.py`
- Modify: `PROJECT_ARCHITECTURE.md`
- Modify: `DEVELOPMENT_LOG.md`
- Modify: `README.md`
- Test: `tests/test_dashboard.py`
- Test: `tests/test_ai_instrument_allocator.py`

**Interfaces:**
- dashboard field `ai_instrument_allocator`
- separate displayed sections for legacy main, old AI sleeve, new allocator sleeve, and short counterfactual

- [ ] Write failing dashboard tests for safe handling of absent/null state and separate $10,000 sleeve, horizon, instrument rationale, costs, and $2,000 affordability fields.
- [ ] Run tests and verify the new section is absent.
- [ ] Add beginner-facing labels and separate strategy/account views without exposing raw secrets or private reasoning.
- [ ] Update natural-language architecture, Mermaid diagrams, startup behavior, and the dated development log.
- [ ] Run dashboard tests and commit `docs: document allocator pipeline and dashboard`.

### Task 9: Full Verification and Repository Audit

**Files:**
- Modify only files required by failures found during verification.

**Interfaces:** None.

- [ ] Run `python -m compileall -q scripts tests`.
- [ ] Run `python -m pytest -q` and require zero failures.
- [ ] Run `python -m scripts.runtime.healthcheck --root .` and record read-only provider readiness.
- [ ] Run mock overnight, premarket, open-execution, monitor, and restart dry runs in a temporary root; assert paper-only mode and zero live-order tool calls.
- [ ] Run `graphify update .` and verify graph output completes.
- [ ] Run `git diff --check`, inspect every changed file, scan tracked/staged content for credential patterns and runtime state, and confirm legacy state/log files are not staged.
- [ ] Commit any verification fixes, then commit remaining reviewed changes with `feat: complete AI instrument allocator v1`.

