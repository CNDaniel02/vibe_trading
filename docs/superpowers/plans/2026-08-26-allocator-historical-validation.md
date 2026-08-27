# Allocator Historical Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an isolated functional and strict historical validation system for `ai_instrument_allocator_v1` without changing forward behavior or its ledger.

**Architecture:** Pure contracts validate immutable point-in-time data and frozen versions; an isolated functional harness drives the production allocator and paper execution lifecycle; a read-only natural replay reports strict funnel results; the Dashboard consumes a precomputed aggregate report.

**Tech Stack:** Python 3.13, pytest, existing JSON/YAML state stores, production paper brokers and WAL, JSON Schema, existing vanilla Dashboard.

## Global Constraints

- Do not edit forward strategy, risk, execution-cost, mode, or scheduler configuration.
- Do not write to the existing `$10,000` allocator state or logs.
- Live broker write calls must remain exactly zero.
- Historical observations later than their decision cutoff are excluded.
- Missing historical values are never filled from current data.
- Historical LLM replay is diagnostic only.
- Executable option PnL is unavailable without a complete point-in-time chain.

---

### Task 1: Point-in-Time and Version Contracts

**Files:**
- Create: `scripts/replay/allocator_validation_contracts.py`
- Test: `tests/test_allocator_historical_validation.py`

**Interfaces:**
- Produces: `build_validation_manifest`, `validate_point_in_time_snapshot`, `assess_market_data_completeness`, `build_walk_forward_partitions`, and `funnel_with_conversion`.

- [ ] Write failing tests for immutable hashes, cutoff rejection, version hashes, missing option-chain fields, and leakage-safe expanding/rolling partitions.
- [ ] Run the focused tests and confirm they fail for missing interfaces.
- [ ] Implement the pure contracts without broker/model/state writes.
- [ ] Run focused tests and confirm all contract tests pass.

### Task 2: Golden Functional Replay

**Files:**
- Create: `fixtures/allocator_validation/bullish_equity.json`
- Create: `fixtures/allocator_validation/bullish_call.json`
- Create: `fixtures/allocator_validation/bearish_put.json`
- Create: `scripts/replay/allocator_functional_replay.py`
- Test: `tests/test_allocator_functional_replay.py`

**Interfaces:**
- Consumes: production `AiInstrumentAllocatorPipeline`, paper brokers, fill WAL, mandates, monitor/exit, trade lifecycle, and `round_trip_cost_decomposition`.
- Produces: `run_golden_path_replay(project_root, scenario_ids=None) -> dict`.

- [ ] Write failing scenario tests that require proposal, plan, revalidation, selected instrument, entry fill, open mandate, exit fill, closed mandate, and PnL attribution.
- [ ] Add a test proving all writes occur below a temporary root and live broker write calls stay zero.
- [ ] Implement deterministic fixture provider and point-in-time adapters.
- [ ] Drive each fixture through the production lifecycle without changing config thresholds.
- [ ] Run focused tests and inspect WAL and lifecycle assertions.

### Task 3: Natural Strict Historical Replay

**Files:**
- Create: `scripts/replay/allocator_historical_replay.py`
- Modify: `scripts/replay/allocator_policy_replay.py`
- Test: `tests/test_allocator_historical_validation.py`

**Interfaces:**
- Consumes: immutable snapshot references and linked historical records.
- Produces: `run_natural_strict_replay(root, hours=48, asof=<required>) -> dict` with counts, conversions, exclusions, and rejection reasons.

- [x] Write failing tests for late news/quote/option-chain exclusion and absent-value fail-closed behavior.
- [x] Write a test proving no historical order/state file is created or modified.
- [x] Implement lineage validation and strict funnel calculation.
- [x] Keep the Issue #3 observed funnel in a separately named report section.
- [x] Run focused replay tests.

### Task 4: Aggregate Report and Dashboard

**Files:**
- Create: `scripts/replay/allocator_validation_report.py`
- Modify: `scripts/dashboard/paper_dashboard.py`
- Modify: `tests/test_dashboard.py`
- Test: `tests/test_allocator_historical_validation.py`

**Interfaces:**
- Produces: a JSON report with `functional_liveness`, `strict_historical_performance`, `walk_forward_readiness`, and `forward_evidence_boundary`.

- [x] Write failing report and Dashboard state tests for all three evidence lanes.
- [x] Implement a CLI with explicit historical cutoff and optional temporary golden replay.
- [x] Load only a precomputed report in Dashboard request handling.
- [x] Add beginner-facing labels that distinguish unavailable executable option PnL from synthetic sensitivity.
- [x] Run Dashboard unit tests.

### Task 5: Documentation and Verification

**Files:**
- Create: `references/allocator_historical_validation.md`
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: `DEVELOPMENT_LOG.md`

- [x] Document commands, evidence boundaries, dataset requirements, and interpretation rules.
- [ ] Run focused replay tests, then the full pytest suite.
- [ ] Run functional and natural replay CLIs and verify `time_violation_count == 0` after exclusion and `live_broker_write_calls == 0`.
- [ ] Compare forward state/log hashes immediately before and after validation.
- [ ] Run Dashboard locally and verify desktop/mobile behavior with Playwright.
- [ ] Run `graphify update .`, review the diff, and scan staged files for secrets/runtime artifacts.
- [ ] Obtain independent code review, push the branch, pass GitHub Actions, and merge.
