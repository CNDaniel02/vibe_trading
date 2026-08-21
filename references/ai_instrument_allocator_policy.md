# AI Instrument Allocator V1 Policy

## Boundary and account identity

`ai_instrument_allocator_v1` is paper-only. It owns the namespace
`state/strategy_sleeves/ai_instrument_allocator_v1/` and starts a new account at
`$10,000` only when that namespaced account does not already exist. Existing
state always wins over configuration on restart.

Readiness and healthcheck commands do not construct the stateful forward
service or initialize this namespace. The namespace is initialized only by an
actual allocator stage or monitor invocation.

The `$2,000` main account and historical `$2,000` AI-gated account are immutable
legacy ledgers. `long_directional_options_v2_weighted` and
`ai_gated_technical_v1` cannot create new entries, but their original open-order,
monitor, and exit paths remain active until flat. The AI-gated research path
continues to write shadow decisions, but actionable output is stopped before
executable quote, risk, or broker work. No migration joins their cash, orders,
fills, PnL, journal, or logs to the allocator.

## Research and execution clock

- 20:00 ET: full evidence research and overnight conditional plans;
- 08:00 ET: active-plan-only incremental evidence update; new evidence runs
  fast News, Challenge, and Decision without discovery or ranking. Each call
  receives the prior signal plus only evidence not present in the prior plan;
- 09:25 ET: active-plan-only evidence invalidation; new evidence runs fast News
  and Challenge only and may retain or veto, but cannot redirect, a plan;
- 09:32 ET: fresh-quote execution with no LLM call;
- regular session: bounded fast research plus five-minute position monitoring.

Research-only stages cannot call either paper broker. An executable allocation
is rebuilt from fresh stock and option quotes and receives a maximum 300-second
authorization window. Exa and Robinhood are read-only observations. No live
Robinhood order tool is present in the pipeline.

`entry_now=false` in an after-hours or premarket model response means "do not
order during research". It does not reject an otherwise valid saved conditional
plan at 09:32. This exception applies only to plans whose recorded source stage
is `overnight`, `premarket_update`, or `preopen_revalidation`; regular-session
fast proposals still require `entry_now=true`. `open_execution` is accepted only
from 09:32 through 09:37 ET, so a late manual invocation cannot execute a stale
opening plan. The 09:25 stage must also persist a same-session
`preopen_revalidated_at` permit, including when no new evidence is found. A
missing permit or failed state write blocks open execution. A newer completed analysis for
the same ticker supersedes the older active plan. A newer fail-closed or
no-trade analysis invalidates the older plan. Every event actually sent to the
successful ranker enters event/ticker cooldown even when it is outside the
deep-analysis top set or the final decision is no-trade. A failed ranking may be
retried but cannot create a plan or order.

Premarket replacement commits the new active plan and the old superseded status
in one state-file write. Evidence refresh or model revalidation failure
invalidates the prior plan before decision-audit append, so restart cannot expose
both plans or execute a stale plan after a partial stage failure.

## Model contract

The ranker may rank only deterministic candidates. News and Challenge may cite
only URLs in the immutable evidence snapshot. Challenge veto is mandatory. The
Decision stage returns exactly one horizon and seven mutually exclusive signed
return buckets whose sum must equal one within `1e-6`.

Python derives bullish, bearish, neutral, dominant bucket, and conservative
move. The conservative move is the probability-weighted mean of the weakest
50% of scenarios within the selected directional mass, not the lower bound of
the single dominant bucket. Raw model values are `uncalibrated`; they may rank
scenarios but cannot be used as real-world probabilities or probability EV.
Calibration is isolated by horizon and uses expanding walk-forward folds
containing only labels that had matured before each test decision. Promotion
primarily compares out-of-sample Brier score and log loss. ECE and reliability
curves are diagnostics.

The API schema intentionally excludes forecast reference fields. After strict
model validation, Python fixes `forecast_reference_price` and
`forecast_reference_time` from the immutable quote in the first research
snapshot. An 08:00 incremental update preserves both fields and the original
horizon. Missing or future reference data fails closed.

Before any `propose_trade` signal can be saved as an actionable plan, Python
also requires a non-empty thesis, entry condition, and invalidation condition;
a non-null `thesis_valid_until` strictly after decision time; and a holding-day
value consistent with the named horizon (`0`, `1`, or `2..5`). Null, missing,
expired, malformed, or inconsistent values become a structured fail-closed
`no_trade`. The allocator repeats this check when consuming persisted state, so
an invalid historical plan cannot reach order creation through a parse error.

`entry_condition` is free-text research and audit context in V1. It is not a
machine-executable trigger and cannot authorize an order. At 09:32 or intraday,
only deterministic Python checks over the current quote, remaining forecast
move, liquidity, spread, authorization lifetime, and account risk can permit an
entry. A model condition that still needs a future price or confirmation must
be `no_trade`.

## Instrument allocation

Bullish signals compare long equity and eligible long calls. Bearish signals
compare eligible long puts. Neutral or weakly dominant signals are no-trade.
The option candidate set is bounded across at most three expirations. A spread
at or below 1.5% is preferred; above 2% is rejected.

The current paper exploration gate requires the selected bullish or bearish
mass to be at least `0.50` and to exceed the second-largest directional mass by
at least `0.15`. This threshold only decides whether an uncalibrated model
scenario may proceed to deterministic instrument comparison. It is not a
calibrated probability, does not authorize an order, and does not relax quote,
liquidity, cost, account, position, or loss limits.

Option comparison uses observed top-of-book and scenario repricing across the
remaining predicted underlying move, elapsed holding time, and configured IV
shifts. At execution, Python keeps the original forecast target and calculates:

```text
forecast_target = reference_price * (1 + conservative_move_pct)
remaining_move = forecast_target / current_price - 1
```

The old forecast is never re-anchored to the current price. If price has already
moved through the conservative target, the adverse remaining scenario normally
causes the executable hurdle to reject the trade.

`planned_exit_at` is calculated once from the XNYS exchange calendar before
allocation and is reused unchanged by both option repricing and the persisted
position mandate. Black-Scholes time decay subtracts the actual elapsed
calendar duration between decision and planned exit, including weekends and
exchange holidays. It does not substitute a one-day trading-session count for
a Friday-to-Monday or holiday-weekend hold.

When a desired-direction option quote has IV, the allocation also records the
nearest-strike candidate's horizon-scaled market-implied move
(`IV * sqrt(elapsed_calendar_days / 365)`), the remaining-forecast/implied-move ratio,
and whether the remaining forecast exceeds that move. This is an explicit
market comparison diagnostic, not a probability EV or an independent model
probability.

Delta, Gamma, Theta, and Vega explain sensitivity; they are not substituted for
multi-day repricing. Each candidate records conservative `scenario_pnl_usd`,
`scenario_return_on_account_nav`, `deterministic_risk_usd`, and deterministic
risk as a share of the same `$10,000` sleeve. The frozen
`deterministic_risk_adjusted_v1` selector ranks eligible candidates by:

```text
selection_score = conservative scenario PnL / deterministic capital at risk
```

For equity, deterministic risk is planned-stop loss; for a long option it is
the full premium. This prevents an option's premium return percentage from
being compared directly with an equity notional return percentage. Raw model
probabilities remain excluded from EV and selection. Scenario pricing and the
paper fill model share the same contract cutoff/tick rounding functions, so a
price above the `$3` cutoff cannot be rounded on two different tick schedules.
Final eligibility still uses conservative repriced return, break-even move,
spread, slippage, and tick cost. Calibration completion does not change paper
fill rules.

## Deterministic risk

- long equity, long call, and long put only;
- stock position notional at most 25% NAV;
- stock planned-stop loss at most 1% NAV;
- option premium at most 3% NAV per entry and 8% NAV aggregate;
- total deployed capital at most 60% NAV;
- at most three executable positions and three entries per session;
- one executable equity or option exposure per underlying;
- no adding, averaging down, all-in, margin, short option, spread, exercise, or
  assignment;
- no same-session re-entry after stop loss or thesis invalidation;
- missing/stale/future quotes, missing IV/Greeks, or invalid state fail closed.

All allocator entry percentages use a current conservative liquidation NAV:
cash plus each existing stock and long-option position marked at its executable
bid. The allocation record saves the calculation time and every position quote
timestamp. If any required holding quote is missing, stale, future-dated,
invalid, or identity-mismatched, no new or retrying entry may proceed. Cost
basis remains an explicit deployment diagnostic; it is never passed as
`nav_usd`.

Live execution and monitor calls preserve `now=None` until all required network
quotes have returned, then validate them against a fresh wall-clock observation
cutoff. The final execution/data cutoff is not earlier than any underlying,
option-candidate, or holding-mark timestamp actually used. An explicitly
supplied replay time never advances; any later observation is rejected as
lookahead.

The deterministic broker risk gate has final veto. The model cannot modify any
limit or create an order object.

## Counterfactuals and mandates

After the `$10,000` allocator selects an instrument, the `$2,000`
counterfactual checks only that exact instrument's affordability, quantity,
risk percentage, and rejection reason. It cannot rerun allocation or select a
different contract.

`short_equity_counterfactual` is a hypothetical direct underlying short used
only as a bearish benchmark. It creates no account, order, or position and its
result is never merged with long-put PnL.

Every allocator entry registers a persisted position mandate before broker
submission. A fill reconciles it to `open`. `intraday_close`, `next_close`, and
`two_to_five_days` map to distinct exchange-session-aware planned exits. An
actionable signal is rejected before order creation unless
`thesis_valid_until >= planned_exit_at`. For allocator-owned positions,
`planned_exit_at` is the hard maximum holding horizon; the legacy generic
calendar-day time stop is suppressed only for this strategy. Price stop,
take-profit, option DTE/expiration/sellout, deterministic mandate invalidation,
and close-of-session force-flatten rules remain active. Legacy strategy exits
are unchanged.

On every restart, recovery runs before normal open-order processing. An
allocator entry left in `created` is cancelled and its plan invalidated; it is
never auto-submitted. A retryable `submitted_to_paper_broker`, `open`, or
`partially_filled` entry may continue only when a valid `pending_fill` or `open`
mandate matches its order id, strategy, exposure id, ticker, and instrument
type. Otherwise the order is cancelled and any matching mandate is closed.
Valid retryable entries and filled entries move their associated plan out of
`active` before plan execution is considered; rejected, expired, and cancelled
entries restore the corresponding terminal plan state. Registration is
idempotent only for the same order/mandate identity and cannot replace an
existing `pending_fill` or `open` mandate for that exposure.

New mandates use `mandate_version: 2` and persist the exact
`max_holding_trading_days`. Registration and every restart-time exit evaluation
verify that `planned_exit_at` is inside the XNYS regular session implied by the
horizon, that the persisted session distance equals the frozen holding-day
count, and that `thesis_valid_until >= planned_exit_at`. Existing V1 mandates
are not migrated or rewritten; they remain readable only when their planned
session is within the horizon's inherent range. Any parseable but contradictory
record fails closed.

For allocator equity positions, the mandate's positive finite
`planned_stop_price` is authoritative for the lifetime of the position. The
monitor does not recompute that stop from a later risk configuration. The
generic percentage stop remains the unchanged default for legacy strategies.

`invalidation_condition` is free-text research and audit context in V1. It is
not polled by an LLM and cannot by itself close a position. The separate
`invalidation_triggered` flag is reserved for a deterministic rule, explicit
manual action, or replay event that calls the mandate invalidation transition.
Missing, inconsistent, expired, malformed, or explicitly invalidated mandates
trigger a fail-closed exit. All plan, allocation, mandate, order, fill, and
close transitions also append JSONL audit events.

Monitor-time mandate validation is bound to the actual position identity, not
only to the mandate's internal fields. `equity:AAPL` must describe equity AAPL
with a positive finite persisted stop. `option:<id>` must match that exact
contract id, underlying ticker, and call/put type. A mismatch produces the same
structured fail-closed exit as other corrupt state and cannot reach field
conversion code.

## Evaluation and logs

Allocator state and logs stay under the namespaced directories. Decisions save
decision/data-cutoff timestamps, model/prompt usage, raw signed buckets, and
calibration metadata. Allocations save all considered executable scenarios,
the fixed forecast reference and target, realized and remaining move, market
implied-move comparison, the selected instrument, `$2,000` affordability, and
separate short benchmark.

Closed-trade accounting verifies:

```text
gross midpoint PnL
- observed spread cost
- adverse slippage and tick cost
- commission
= executable net PnL
```

The identity residual must remain near zero. Forward paper results, not replay,
are the primary promotion evidence.

## Revision record

- 2026-08-20: added actionable-signal fail-closed semantics, account-risk
  normalized cross-instrument selection, exchange-calendar elapsed option
  repricing, and shared scenario/fill tick rounding.
- 2026-08-20: made exchange-session mandates authoritative for allocator time
  exits, required thesis validity to cover the full declared horizon, and
  documented free-text invalidation as audit-only.
- 2026-08-20: added mandate V2 exact session-distance validation and made the
  persisted allocator equity stop authoritative across restarts/config changes.
- 2026-08-20: added fail-closed orphan-order recovery, exposure-bound mandate
  validation, bid-marked entry NAV, and the audit-only `entry_condition`
  boundary.
- 2026-08-21: completed post-submit plan recovery, active-mandate overwrite
  protection, live post-fetch quote cutoffs, and persisted-position identity
  validation.
