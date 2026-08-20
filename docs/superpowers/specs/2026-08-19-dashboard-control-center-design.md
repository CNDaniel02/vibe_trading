# Dashboard Control Center Design

## Goal

Replace the current single long paper-trading report with a beginner-friendly,
read-only control center that answers five questions quickly:

1. Is the paper system running with fresh data?
2. Are the paper accounts making or losing money?
3. What positions and orders exist now?
4. What did each strategy decide, and why did it not trade?
5. Is a current operational fault blocking decisions?

The redesign changes presentation and dashboard log-reading performance only.
It does not change strategy, risk, broker, scheduler, account, or order behavior.

## Navigation

The dashboard uses five client-side tabs in this order:

- `总览`
- `持仓与订单`
- `策略表现`
- `AI 决策`
- `系统健康`

The selected tab is represented in the URL hash. The default is `总览`.
Tabs use native buttons with ARIA tab semantics and keyboard navigation.

## Information Architecture

### Persistent Header

The header always shows:

- product name: `模拟交易控制台`;
- an explicit `Paper only` boundary;
- service state;
- market session;
- data freshness and last refresh time.

No order, restart, broker-write, or configuration button is added.

### Overview

The first viewport contains:

- a plain-language `系统现在在做什么` status;
- the legacy `$2,000` account and the independent `$10,000` allocator account;
- daily and cumulative PnL;
- open-position and open-order counts;
- deployed capital or available cash when present;
- current operational alerts only;
- a compact strategy status table.

Historical incident counts do not appear as current failures. They remain
available in the health tab with explicit historical labeling.

### Positions And Orders

Equity and option exposure share one operational table while preserving their
account and strategy ownership. Rows show instrument, direction, quantity,
entry or limit price, status, PnL when available, and exit mandate. Created or
open orders are never presented as positions.

### Strategy Performance

Each strategy is one comparable row, not a large descriptive card. It shows:

- executable, shadow-only, exit-management-only, or disabled mode;
- account ownership;
- decisions, entries, closed trades, PnL, win rate when available;
- the most recent no-trade or fail-closed reason.

### AI Decisions

This tab shows allocator decisions, Exa/news-drift evidence, candidate ranking,
the structured decision, and deterministic risk outcome. It never exposes raw
private chain-of-thought or API credentials.

### System Health

Health is grouped by current status: runtime, market data, scheduler,
Robinhood read-only access, Exa, DeepSeek, audit trail, and paper-only safety.
Current errors lead; historical totals and advanced metrics are secondary.

## Visual System

- Quiet operational layout with a neutral light background and white surfaces.
- Green is reserved for healthy/profitable state, red for loss/current failure,
  amber for attention, and blue for selection or neutral information.
- Cards use at most a 6px radius and are not nested.
- Tables become labeled rows on narrow screens.
- Numeric columns use tabular figures. Text never scales with viewport width.
- No gradients, decorative illustrations, chart dependency, or frontend
  framework is introduced.

## Data And Performance

The existing read-only Python server and `/api/state` contract remain. The
frontend polls every 15 seconds and pauses polling while the document is
hidden. Manual browser reload remains sufficient for an immediate refresh.

`_read_jsonl` is changed from whole-file loading to bounded reverse tail reads.
This prevents every dashboard refresh from reading the complete large audit,
decision, and runtime logs. Invalid or concurrently appended trailing JSON is
still ignored until the next refresh.

## Failure Behavior

- Missing or null state renders an explicit unavailable value, not a crash.
- A failed API response leaves a visible read error in the active view.
- Unknown statuses use neutral styling rather than being labeled healthy.
- The dashboard remains read-only: only `GET`, `HEAD`, and `OPTIONS` routes are
  exposed; no broker adapter is imported.

## Verification

- Unit tests cover bounded JSONL tail reads, null data, read-only routes, tab
  structure, paper boundary text, current-versus-historical issue labels, and
  absence of raw reasoning content.
- The full Python test suite must pass.
- Playwright verifies desktop and mobile layouts, tab interaction, no horizontal
  overflow, no console errors, and nonblank content in every tab.

