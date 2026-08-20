# Dashboard Control Center Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a beginner-friendly five-tab read-only paper-trading control center without changing any trading behavior.

**Architecture:** Keep the existing standard-library HTTP server and `/api/state` data contract. Replace only the active `_BEGINNER_PAGE`, improve the existing JSONL reader in place, and reuse `build_dashboard_state` rather than introducing a frontend framework or a second dashboard API.

**Tech Stack:** Python 3.13 standard library, existing pytest suite, native HTML/CSS/JavaScript, Playwright CLI.

## Global Constraints

- The dashboard is read-only and must never import or call live broker order tools.
- Preserve paper account, order, position, strategy, risk, and scheduler behavior unchanged.
- Use five tabs in this exact order: `总览`, `持仓与订单`, `策略表现`, `AI 决策`, `系统健康`.
- Keep the legacy `$2,000` ledger separate from the independent `$10,000 ai_instrument_allocator_v1` sleeve.
- Do not expose API keys, raw private chain-of-thought, or `reasoning_content`.
- Use no new frontend framework, chart library, or runtime dependency.
- Poll at 15-second intervals and pause polling while the document is hidden.
- Support desktop and 390px mobile widths without horizontal page overflow.

---

### Task 1: Bounded Dashboard Log Reads

**Files:**
- Modify: `scripts/dashboard/paper_dashboard.py`
- Test: `tests/test_dashboard.py`

**Interfaces:**
- Consumes: `_read_jsonl(path: Path, limit: int = 400)` callers already in `build_dashboard_state`.
- Produces: the same `list[dict[str, Any]]` return contract while reading only a bounded tail of large files.

- [ ] **Step 1: Write failing tests**

Add tests proving `_read_jsonl` returns only the last requested valid records,
ignores a partial trailing record, handles UTF-8, and does not call
`Path.read_text`.

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `python -m pytest tests/test_dashboard.py -k read_jsonl -q`

Expected: failure because the current implementation calls `Path.read_text`.

- [ ] **Step 3: Implement the bounded reverse reader**

Open the file as binary, seek from the end in fixed-size chunks until enough
newlines are available, discard a partial leading line when the read did not
start at byte zero, decode complete lines as UTF-8, and retain the existing
invalid-JSON behavior.

- [ ] **Step 4: Verify GREEN**

Run: `python -m pytest tests/test_dashboard.py -k read_jsonl -q`

Expected: all selected tests pass.

### Task 2: Five-Tab Dashboard Shell And Overview

**Files:**
- Modify: `scripts/dashboard/paper_dashboard.py`
- Test: `tests/test_dashboard.py`

**Interfaces:**
- Consumes: existing `/api/state` JSON and `beginner_summary`.
- Produces: `_BEGINNER_PAGE` with ARIA tabs, URL-hash selection, persistent paper/service header, and a compact overview.

- [ ] **Step 1: Write failing markup contract tests**

Assert the active page includes exactly five named tab buttons, corresponding
tab panels, `role="tablist"`, the paper-only boundary, a 15000ms refresh
interval, and hidden-document polling protection.

- [ ] **Step 2: Run focused tests and verify RED**

Run: `python -m pytest tests/test_dashboard.py -k "tab or polling or page" -q`

Expected: failure because the current page has no tabs and polls every 5 seconds.

- [ ] **Step 3: Implement the shell and overview**

Replace `_BEGINNER_PAGE` with native CSS and JavaScript that renders the header,
tabs, system activity, both paper accounts, daily/cumulative results, current
alerts, and compact strategy rows. Reuse existing state fields and sanitizers.

- [ ] **Step 4: Verify GREEN**

Run: `python -m pytest tests/test_dashboard.py -q`

Expected: all dashboard tests pass.

### Task 3: Operational Detail Tabs

**Files:**
- Modify: `scripts/dashboard/paper_dashboard.py`
- Test: `tests/test_dashboard.py`

**Interfaces:**
- Consumes: positions, orders, option positions/orders, strategy metrics,
  allocator decisions, news-drift state, heartbeat, and existing issue summary.
- Produces: populated `持仓与订单`, `策略表现`, `AI 决策`, and `系统健康` panels.

- [ ] **Step 1: Write failing content and safety tests**

Assert all four panels expose their required headings and empty states, the
health panel separates current alerts from historical counts, order rows remain
distinct from positions, and no raw `reasoning_content` appears in rendered
state or page source.

- [ ] **Step 2: Run focused tests and verify RED**

Run: `python -m pytest tests/test_dashboard.py -k "positions or strategy or ai or health or reasoning" -q`

Expected: at least one failure for missing tab content.

- [ ] **Step 3: Implement the detail views**

Render responsive operational tables and empty states from existing state.
Translate common deterministic rejection reasons to plain Chinese. Preserve
the raw machine value only in advanced health details when it is already safe.

- [ ] **Step 4: Verify GREEN**

Run: `python -m pytest tests/test_dashboard.py -q`

Expected: all dashboard tests pass.

### Task 4: Browser QA, Documentation, And Final Review

**Files:**
- Modify: `README.md`
- Modify: `DEVELOPMENT_LOG.md`
- Modify: `PROJECT_ARCHITECTURE.md`

**Interfaces:**
- Consumes: completed dashboard and local state files.
- Produces: verified desktop/mobile dashboard, updated operator documentation,
  and a review-ready diff.

- [ ] **Step 1: Update documentation**

Document the five views, the 15-second visibility-aware refresh, the bounded log
tail reader, the dashboard-only start/stop command, and the unchanged read-only
boundary. Append a dated development-log entry rather than rewriting history.

- [ ] **Step 2: Run dashboard and full tests**

Run: `python -m pytest -q`

Expected: the complete suite passes.

- [ ] **Step 3: Run browser verification**

Start the dashboard on `127.0.0.1:8790`. With Playwright, verify every tab at
1440x1000 and 390x844, capture screenshots under `output/playwright/`, assert
`document.documentElement.scrollWidth <= innerWidth`, and check console errors.

- [ ] **Step 4: Refresh the knowledge graph and review the diff**

Run: `graphify update .`

Then request a Luna Max whole-change review covering correctness, safety,
accessibility, responsive behavior, and unnecessary complexity. Resolve all
critical or important findings before completion.

