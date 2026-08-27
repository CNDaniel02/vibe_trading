# Allocator Historical Validation Design

## Purpose

Build an isolated validation system for the frozen `ai_instrument_allocator_v1`
implementation on `main`. The system must answer three different questions
without conflating their evidence:

1. **Functional liveness:** can the production allocator path complete an
   equity, call, and put round trip under fixed point-in-time fixtures?
2. **Historical performance:** what does a strict natural replay do when only
   information visible at each decision cutoff is admitted?
3. **Forward evidence:** what actually happened in the existing `$10,000`
   forward paper sleeve?

Functional success is not profitability evidence. Historical LLM output is
diagnostic/comparative evidence only. Forward state remains the sole source of
forward paper evidence.

## Global Safety Boundary

- Do not modify forward strategy parameters, prompt content, risk limits,
  scheduler behavior, or the existing `$10,000` paper ledger.
- Functional replay always creates a temporary root, copies only versioned
  configuration, and uses the production allocator namespace inside that
  isolated root.
- Natural replay is read-only against source snapshots and writes reports only
  to an explicitly supplied output path outside `state/` and `logs/`.
- No validation component imports or exposes live order placement tools.
- Every report records `live_broker_write_calls: 0` and verifies that the paper
  broker path reports `live_order_tools_called: false`.
- No current quote, news, option chain, or account value may fill a historical
  gap.

## Architecture

### Validation Contracts

`scripts/replay/allocator_validation_contracts.py` owns immutable snapshot
validation, version manifests, funnel math, data-completeness assessment, and
walk-forward partition rules. It has no broker, model, or state-store access.

The version manifest contains:

- strategy version and source revision;
- prompt version and SHA-256 hashes for every allocator prompt;
- schema version and schema hash;
- SHA-256 hashes for all strategy/risk/execution configuration files;
- model id and provider mode;
- replay data cutoff and immutable dataset hash.

### Golden-Path Functional Replay

`scripts/replay/allocator_functional_replay.py` loads three committed fixtures:
`bullish_equity`, `bullish_call`, and `bearish_put`. A deterministic fixture
provider and point-in-time market adapters drive the production
`AiInstrumentAllocatorPipeline` through:

`proposal -> plan -> pre-open revalidation -> allocation -> deterministic risk
-> PaperBroker/OptionPaperBroker -> fill WAL -> mandate -> monitor/exit ->
TradeLifecycleJournal -> realized PnL attribution`.

The harness does not duplicate broker, fill, mandate, exit, or PnL logic. It
asserts all writes remain below a newly created temporary root and deletes that
root after returning a summarized report unless a caller explicitly asks to
retain it for debugging.

### Natural Strict Historical Replay

`scripts/replay/allocator_historical_replay.py` consumes immutable allocator
snapshots and linked decision/allocation/order/fill records. It verifies hashes
and rejects an entire decision lineage when any linked observation is later
than the decision cutoff. Missing fields remain missing. Current adapters are
never called.

The replay reports the natural funnel:

`candidate -> watch/no_trade/proposal -> allocation -> selected instrument ->
paper order -> paper fill`

Each stage includes count, conversion from the preceding stage, and typed
rejection reasons. The report embeds, but does not merge with, the Issue #3
observed audit funnel.

Historical model replay is optional and disabled by default. If enabled later,
it must use the frozen provider/model/prompt/schema manifest, write only to an
isolated replay root, and remain labelled diagnostic.

### Data Completeness and Walk-Forward

Equity executable PnL requires point-in-time bid/ask plus corporate-action-safe
OHLCV. Option executable PnL additionally requires the full chain observed at
that time, including contract identity, bid/ask, IV, Delta, Gamma, Theta, Vega,
volume, open interest, and timestamps. A missing required option field makes
option performance unavailable; the report may show only synthetic option
sensitivity and must label it non-executable.

Walk-forward partitions support expanding and rolling training windows. For
every horizon, development, calibration, and final holdout are disjoint. A
training/calibration row is eligible only when its label matured before the
next decision cutoff. Final holdout rows are never used for model selection or
calibration.

### Dashboard

The Dashboard loads a cached validation report without executing a functional
replay on an HTTP request. It displays three separately labelled evidence cards:

- functional liveness by scenario;
- strict historical performance and data availability;
- forward paper evidence from the live sleeve.

Synthetic option sensitivity is never displayed as executable option PnL.

## Acceptance Evidence

- all three golden fixtures complete the production path and close with PnL;
- strict replay has admitted `time_violation_count == 0` after exclusions and
  reports excluded source violations separately;
- validation records no live broker write calls;
- before/after hashes show the forward allocator state/logs were not changed by
  validation commands;
- full local pytest and GitHub Actions pass;
- desktop and mobile Dashboard checks show no overflow or console errors;
- reports explicitly label functional, historical, and forward evidence.
