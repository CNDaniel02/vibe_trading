# Weighted and AI-Gated Paper Strategy Policy

## Active deterministic equity strategy

`weighted_relative_strength_v2` replaces the all-AND entry decision without
removing safety gates. A quote must be valid and fresh, the session must be
regular, completed OHLCV must reach the expected prior NYSE session, the ticker
must not already be held, and extreme chase risk remains prohibited.

Relative strength, one-day momentum, five-day momentum, volume confirmation,
market regime, and chase quality are soft features. Their weighted sum controls
entry. The original `relative_strength_v1` output is recorded against the same
snapshot as a shadow baseline.

Weights stay fixed until at least 100 valid one-hour outcome labels mature.
Only one overlapping label per ticker per hour is admitted, targets outside
regular hours are not scheduled, and quotes arriving more than 15 minutes after
the target expire without training. The adaptive mode uses a new versioned
state file, minimizes average squared feature loss using exponential
reweighting, and retains a non-zero floor. It cannot add features, change risk
limits, or use future data.

## Active deterministic options strategy

`long_directional_options_v2_weighted` permits only fully paid long calls and
long puts. Company-specific negative evidence and downside technical strength
can support a put in neutral or risk-on broad-market regimes. Broad-market
regime is a soft feature, not a mandatory direction switch.

Earnings exclusion, contract liquidity, spread, DTE, Greeks, IV, premium,
position count, cash, and shared deployment caps remain deterministic vetoes.
Every contract-selection rejection category is logged. When no directional
event exists, technical scores retain their full weight; missing news is
neutral rather than an automatic 30% score penalty.

## AI-gated executable paper strategy

`ai_gated_technical_v1` has an isolated `$2,000` virtual account. It does not
share positions, orders, daily counters, or performance statistics with the
active deterministic account. Equity and options inside the AI sleeve do share
that sleeve's cash and risk limits.

The cycle is:

1. Collect bounded read-only Robinhood watchlist, scan, and earnings candidates.
2. Deterministically rank both bullish and bearish technical opportunity,
   reserving bounded slots for confirmed reported-earnings surprises.
3. Search Exa for the top 5-8 candidates with a 48-hour cutoff.
4. Ask DeepSeek for one low-cost structured ranking.
5. Obtain primary-source verification for at most two deep candidates.
6. Run News/Bull, Challenge, and Decision; Challenge has mandatory veto.
7. Require a deterministic confidence floor and all existing risk checks.
8. Route only to the namespaced local paper broker.
9. Monitor, exit, journal, and evaluate the sleeve independently.

Exa discovery uses low-latency search with inline token-bounded highlights and
content no older than 24 hours. Only the final primary-source verification uses
the balanced `auto` search mode. Exa Agent and Monitors are not used because
DeepSeek and APScheduler already own those responsibilities; full deep search
is deferred until measured evidence-grounding evals justify its added cost.

Within 90 minutes before the open, the same pipeline may create a research-only
plan. It cannot create an order, publish an executable signal, or consume event
cooldown state. A separate 09:32 New York job repeats the analysis with a fresh
regular-session quote and all deterministic checks before any local paper order.

No model may call broker tools, change configuration, add an unvalidated ticker,
or emit a live order. The only permitted option actions are buy-to-open and
sell-to-close for one long call or put.

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
