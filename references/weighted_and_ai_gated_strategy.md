# Weighted and AI-Gated Paper Strategy Policy

## Shadow deterministic equity strategy

`weighted_relative_strength_v2` replaces the all-AND candidate decision without
removing safety gates. It is currently `shadow_only` because observed forward
returns were negative after execution costs. A quote must be valid and fresh, the session must be
regular, completed OHLCV must reach the expected prior NYSE session, the ticker
must not already be held, and extreme chase risk remains prohibited.

Relative strength, one-day momentum, five-day momentum, volume confirmation,
market regime, and chase quality are soft features. Their weighted sum controls
entry. The original `relative_strength_v1` output is recorded against the same
snapshot as a shadow baseline.

Adaptive weights are currently disabled. New observations target 360-minute
net returns so the label horizon matches the prior holding period. Targets outside
regular hours are not scheduled, and quotes arriving more than 15 minutes after
the target expire. The strategy cannot return to `paper_broker` until new
out-of-sample labels show a positive edge after spread and slippage.

## Entry-frozen deterministic options strategy

`long_directional_options_v2_weighted` has `new_entries_enabled: false`.
Existing open orders and positions remain under its original monitor and exit
logic until flat. Its scores remain available for comparison, but it cannot
create another entry.

Earnings exclusion, contract liquidity, spread, DTE, Greeks, IV, premium,
position count, cash, and shared deployment caps remain deterministic vetoes.
Entry spread may not exceed 2%, with 1.5% treated as preferred. Every contract-selection rejection category is logged. When no directional
event exists, technical scores retain their full weight; missing news is
neutral rather than an automatic 30% score penalty.

## Entry-frozen AI-gated paper strategy

`ai_gated_technical_v1` has an isolated historical `$2,000` virtual account. It does not
share positions, orders, daily counters, or performance statistics with the
active deterministic account. Equity and options inside the AI sleeve do share
that sleeve's cash and risk limits. New entries are disabled; open-order
processing, position monitoring, and exits continue, and the bounded research
path records non-executing shadow decisions. Historical state and logs are not
migrated into the new allocator.

The cycle is:

1. Collect bounded read-only Robinhood watchlist, scan, and earnings candidates.
2. Deterministically rank both bullish and bearish technical opportunity,
   reserving bounded slots for confirmed reported-earnings surprises.
3. Search Exa for the top 5-8 candidates with a 48-hour cutoff.
4. Ask DeepSeek for one low-cost structured ranking.
5. Obtain primary-source verification for at most three deep candidates, with
   two available slots reserved for ranked bearish opportunities.
6. Run News/Bull and Challenge without thinking, then final Decision with
   DeepSeek V4 Flash thinking; Challenge has mandatory veto.
7. Require `entry_now`, numeric entry bounds, and an expiry no more than five
   minutes after the decision. Refresh the quote and reject unmet conditions.
8. Block a ticker for the rest of the session after a stop-loss exit.
9. Require a deterministic confidence floor and all existing risk checks.
10. Route only to the namespaced local paper broker.
11. Monitor, exit, journal, and evaluate the sleeve independently by bullish
    and bearish direction.

Exa discovery uses low-latency search with inline token-bounded highlights and
content no older than 24 hours. Only the final primary-source verification uses
the balanced `auto` search mode. Exa Agent and Monitors are not used because
DeepSeek and APScheduler already own those responsibilities; full deep search
is deferred until measured evidence-grounding evals justify its added cost.

The workflow below now remains active for shadow comparison. The entry-frozen
runtime stops actionable output before executable quote refresh, deterministic
entry risk, or order creation.

No model may call broker tools, change configuration, add an unvalidated ticker,
or emit a live order. The only permitted option actions are buy-to-open and
sell-to-close for one long call or put.

## AI instrument allocator V1

`ai_instrument_allocator_v1` replaces new AI entry generation without touching
either legacy ledger. It has an isolated `$10,000` paper sleeve. The model emits
one complete signed-return bucket distribution for `intraday_close`,
`next_close`, or `two_to_five_days`; Python derives direction and magnitude.
Raw values remain uncalibrated and do not produce probability EV.

Python compares long equity and long calls for bullish signals, or long puts for
bearish signals. Multi-day option comparison uses underlying/time/IV scenario
repricing with Vega diagnostics, not a local Delta/Gamma/Theta approximation.
The executable allocator ranks eligible instruments on conservative scenario
PnL divided by deterministic capital at risk using the same sleeve NAV, rather
than directly comparing equity notional return with option premium return.
Scenario decay uses the exchange-calendar-derived `planned_exit_at`, including
weekends and holidays, and scenario/fill paths share one contract tick rule.
Every selected instrument then passes the same deterministic broker risk gate.
The `$2,000` counterfactual checks that exact selected instrument only.
`short_equity_counterfactual` remains a no-account, no-order shadow benchmark.

## Runtime and promotion

Network-bound jobs run as supervised subprocesses. Timeouts kill the complete
child process tree and mark the parent heartbeat degraded. JSONL writes use a
cross-process lock and durable append.

The supervisor coordinates jobs by explicit resources instead of one global
mutation flag. The deterministic/catalyst lines share `main_account`, the AI
line uses `ai_account`, both research lines share `evidence_store`, and EOD
owns both accounts while flattening. Resource-conflict skips are persisted to
`runtime_jobs.jsonl`. Catalyst and AI research start offsets are separated so
their hourly evidence-store work does not collide by construction. AI position
monitoring is a separate bounded job and is not coupled to the deterministic
forward cycle.

The legacy baseline-gated multi-agent comparison remains shadow-only and is
bounded to the highest-scoring active candidate per forward cycle. The
independent AI-gated strategy retains its own top-set and deep-research budget.

No strategy is considered profitable from a small sample. Promotion requires
the thresholds in `config/evaluation.yaml`, with forward paper results as the
primary evidence.
