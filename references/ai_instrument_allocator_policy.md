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
  fast News, Challenge, and Decision without discovery or ranking;
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
move. Raw model values are `uncalibrated`; they may rank scenarios but cannot be
used as real-world probabilities or probability EV. Calibration is isolated by
horizon and uses expanding walk-forward folds containing only labels that had
matured before each test decision. Promotion primarily compares out-of-sample
Brier score and log loss. ECE and reliability curves are diagnostics.

## Instrument allocation

Bullish signals compare long equity and eligible long calls. Bearish signals
compare eligible long puts. Neutral or weakly dominant signals are no-trade.
The option candidate set is bounded across at most three expirations. A spread
at or below 1.5% is preferred; above 2% is rejected.

Option comparison uses observed top-of-book and scenario repricing across the
predicted underlying move, elapsed holding time, and configured IV shifts.
Delta, Gamma, Theta, and Vega explain sensitivity; they are not substituted for
multi-day repricing. Final comparison uses conservative repriced net return,
break-even move, spread, slippage, and tick cost. Calibration completion does
not change paper fill rules.

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
`two_to_five_days` map to distinct planned exits. Missing, inconsistent,
expired, or invalidated mandates trigger a fail-closed exit. All plan,
allocation, mandate, order, fill, and close transitions also append JSONL audit
events.

## Evaluation and logs

Allocator state and logs stay under the namespaced directories. Decisions save
decision/data-cutoff timestamps, model/prompt usage, raw signed buckets, and
calibration metadata. Allocations save all considered executable scenarios,
the selected instrument, `$2,000` affordability, and separate short benchmark.

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
