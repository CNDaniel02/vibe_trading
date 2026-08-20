# auto-trading-skill

Equity and long-premium options paper/shadow trading system. It observes real market data but routes every order to local virtual accounts. Live trading is not implemented.

Detailed Chinese documentation:

- [`PROJECT_ARCHITECTURE.md`](PROJECT_ARCHITECTURE.md): complete architecture, account isolation, runtime, equity/options/AI pipelines, state, evaluation, and known limitations.
- [`DEVELOPMENT_LOG.md`](DEVELOPMENT_LOG.md): append-only development and runtime repair history. Every behavior-changing update must add a new entry at the top.
- [`references/ai_instrument_allocator_policy.md`](references/ai_instrument_allocator_policy.md): normative allocator account, model, repricing, risk, mandate, counterfactual, calibration, and logging policy.

The paper broker supports fractional equity quantities in increments of `0.001` shares. Position and order caps still apply before an order is created.

## Safety Boundary

- `paper: true`
- `live_readonly: false`
- `live_trading: false`
- `weighted_relative_strength_v2` is shadow-only while its net-of-cost forward edge is negative.
- `long_directional_options_v2_weighted` no longer opens new entries; its existing orders and positions remain under the legacy monitor and exit logic until flat.
- `relative_strength_v1` and `long_directional_options_v1` remain unchanged deterministic shadow baselines.
- `multi_agent_relative_strength_v2_candidate` and Vibe Swarm are shadow/research only.
- `exa_deepseek_catalyst_v1` independently discovers candidates but remains shadow-only and creates no orders.
- `llm_news_drift_v1` discovers market-wide news before any technical screen and remains an isolated long-equity shadow experiment.
- `ai_gated_technical_v1` no longer opens new entries; its existing `$2,000` sleeve is preserved byte-for-byte and remains exit-managed until flat.
- `ai_instrument_allocator_v1` is the only new AI executable experiment. It uses a separate `$10,000` paper sleeve and compares long equity, long call, and long put after deterministic scenario repricing and risk veto.
- No adapter exposes create, submit, place, or cancel methods for a real broker.
- Options sell-to-open, short contracts, spreads, margin, 0DTE, exercise, and assignment are rejected.

## Architecture

```text
Vibe OHLCV + Robinhood MCP equity/options data + Exa news
                 |
        immutable timestamped snapshot
                 |
 deterministic validation + weighted technical scoring
        |                         |
 equity 360m labels     options direction + contract filter
        |                         |
 shadow evaluation          options/shared risk gate
                                  |
                         local options paper broker
```

Screened equities also flow through the preserved shadow comparison. LLM output
never directly creates an order.

In parallel, `exa_deepseek_catalyst_v1` runs independently of baseline screening:

```text
core watchlist + market-wide earnings + saved read-only scans + Exa market events
        -> low-cost candidate extraction and structured ranking
        -> ticker instrument validation + timestamped evidence snapshot
        -> non-thinking Bull/News + Challenge -> thinking Decision
        -> deterministic equity or long-option risk veto
        -> shadow proposal and catalyst journal only
```

`ai_gated_technical_v1` is a separately measurable executable paper lane:

```text
read-only watchlist/scans/earnings -> deterministic technical top 5-8
        -> bounded parallel Exa evidence searches
        -> low-cost DeepSeek ranking
        -> News/Bull + Challenge in non-thinking mode
        -> thinking Decision + executable price/time contract
        -> deterministic equity/options/shared-risk veto
        -> isolated local paper sleeve, monitor, exit, journal, and metrics
```

That lane is now entry-frozen. Its monitor stays active solely to close legacy
orders and positions. New AI research and entries use `ai_instrument_allocator_v1`:

```text
read-only scans and technical top 8 + Exa evidence
        -> low-cost ranker -> News -> Challenge -> signed-return buckets
        -> Python derives direction and conservative move (raw probabilities remain uncalibrated)
        -> fresh stock quote + bounded option candidates across expirations
        -> stock/IV/time scenario repricing and executable-cost comparison
        -> deterministic shared risk veto
        -> isolated $10,000 paper sleeve + horizon-aware position mandate
        -> separate $2,000 same-instrument affordability check
        -> separate short_equity_counterfactual shadow benchmark
```

`llm_news_drift_v1` is a faster, price-blind experiment:

```text
one rotating market-wide Exa search at most every 15 minutes
        -> immutable raw snapshot + URL/content/event deduplication
        -> one headline-only DeepSeek structured classification
        -> exact ticker validation + Robinhood bid/ask/fundamentals
        -> deterministic latency/liquidity/spread/chase checks
        -> long-equity shadow proposal only
        -> +1m/+5m/+15m/close/next-close/second-close labels
        -> event, firm-day, portfolio-day, and cost-sensitivity metrics
```

The continuous service is a supervisor. Every network-bound cycle runs in a
child process with a hard deadline and process-tree cleanup. The parent writes
its own heartbeat, verifies the owning PID lock, runs a separate EOD guard, and
cannot stay falsely healthy after a Robinhood MCP or model call hangs. Jobs use
explicit account/evidence resources, so the main paper line and isolated AI
paper sleeve can run concurrently without racing on shared state.

Vibe is pinned at `6fc038d37f1767ae429bab435654b9b425ae66f4`. Its source is not copied; an isolated subprocess adapter provides OHLCV, independent backtests, and optional read-only Swarm research.

## Architecture decision

The project-owned API orchestration is the sole path eligible for promotion beyond shadow research. Codex/TUI and Vibe remain read-only research sidecars; neither can receive direct broker control. See `references/architecture_decision_002_api_orchestrated_research.md`.

## Local Setup

```powershell
cd auto-trading-skill
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env.local
```

Put secrets only in `.env.local` or process environment variables. Do not put keys in YAML.

Required for continuous forward evaluation:

```text
EXA_API_KEY
OPENAI_API_KEY
OPENAI_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-v4-flash
```

Then authorize the project's read-only Python MCP client once:

```powershell
.\.venv\Scripts\python.exe -m scripts.broker.robinhood_mcp_audit
```

If readiness reports that authorization is required, renew the stored session
interactively:

```powershell
.\.venv\Scripts\python.exe -m scripts.broker.robinhood_mcp_audit --reset-credentials
```

The OAuth token and client registration are stored only in a current-user DPAPI-encrypted file under `state/`. The audit verifies the complete 50-tool manifest. Runtime calls use an explicit read-only allowlist for quotes, historicals, fundamentals, financials, technical indicators, earnings, saved scans, instrument search, and option market data. Scanner creation/update and all order tools remain unavailable.

Alpaca is enabled as a standby market-data source when `ALPACA_API_KEY_ID` and
`ALPACA_API_SECRET_KEY` are present. Robinhood MCP remains the primary quote
provider. A bounded Robinhood failure automatically falls back to Alpaca IEX
when `forward_data.fallback_quote_provider: alpaca`; the effective provider and
each data-collection stage are written to append-only runtime logs.

## Commands

```powershell
# All tests
.\.venv\Scripts\python.exe -m pytest -q

# No-network end-to-end fixture dry run
.\.venv\Scripts\python.exe -m scripts.orchestrator.dry_run_forward_pipeline

# No-network long-put options paper lifecycle dry run
.\.venv\Scripts\python.exe -m scripts.orchestrator.dry_run_options_pipeline

# No-network independent catalyst discovery dry run
.\.venv\Scripts\python.exe -m scripts.orchestrator.dry_run_catalyst_pipeline

# Read-only credential and integration readiness; does not initialize a sleeve
.\.venv\Scripts\python.exe -m scripts.orchestrator.forward_paper_service --readiness

# One real forward paper cycle; fails closed outside regular NYSE hours
.\.venv\Scripts\python.exe -m scripts.orchestrator.forward_paper_service --once

# One real Exa + DeepSeek discovery cycle; any order is local paper state only
.\.venv\Scripts\python.exe -m scripts.orchestrator.forward_paper_service --catalyst-once

# One real AI-gated cycle; any order is local and uses the isolated paper sleeve
.\.venv\Scripts\python.exe -m scripts.orchestrator.forward_paper_service --ai-gated-once

# One real market-wide news-drift cycle; creates shadow proposals but no orders
.\.venv\Scripts\python.exe -m scripts.orchestrator.forward_paper_service --news-drift-once

# Monitor and exit the isolated AI paper sleeve without starting discovery
.\.venv\Scripts\python.exe -m scripts.orchestrator.forward_paper_service --ai-monitor-once

# New allocator conditional research stages (no order before open_execution)
.\.venv\Scripts\python.exe -m scripts.orchestrator.forward_paper_service --allocator-stage overnight
.\.venv\Scripts\python.exe -m scripts.orchestrator.forward_paper_service --allocator-stage premarket_update
.\.venv\Scripts\python.exe -m scripts.orchestrator.forward_paper_service --allocator-stage preopen_revalidation

# 09:32-style fresh-quote execution; this stage makes zero LLM calls
.\.venv\Scripts\python.exe -m scripts.orchestrator.forward_paper_service --allocator-stage open_execution

# Bounded regular-session fast research and horizon-aware monitor
.\.venv\Scripts\python.exe -m scripts.orchestrator.forward_paper_service --allocator-stage intraday
.\.venv\Scripts\python.exe -m scripts.orchestrator.forward_paper_service --allocator-monitor-once

# EOD/overnight recovery guard
.\.venv\Scripts\python.exe -m scripts.orchestrator.forward_paper_service --eod-once

# Refresh metrics and Markdown report without loading broker adapters
.\.venv\Scripts\python.exe -m scripts.orchestrator.forward_paper_service --evaluate-once

# Process/PID/heartbeat health; expected to fail when the service is stopped
.\.venv\Scripts\python.exe -m scripts.runtime.healthcheck --require-heartbeat

# Four-call fixture-only API pilot; no market-data or order calls
.\.venv\Scripts\python.exe -m scripts.evaluation.run_ai_gated_api_pilot

# One-call price-blind news-drift API pilot; no Exa, market-data, or order calls
.\.venv\Scripts\python.exe -m scripts.evaluation.run_news_drift_api_pilot

# Continuous supervised APScheduler service
.\.venv\Scripts\python.exe -m scripts.orchestrator.forward_paper_service

# The continuous service prints one compact JSON status at startup and after
# each cycle. Press Ctrl+C to stop it gracefully and release its process lock.
# Standalone state-mutating commands fail closed while this service owns the
# lock. Stop the service before running --once, --catalyst-once,
# --ai-gated-once, --ai-monitor-once, --allocator-stage,
# --allocator-monitor-once, --news-drift-once, or --eod-once manually.

# Read-only local GUI (run in a separate terminal while the service is running)
.\.venv\Scripts\python.exe -m scripts.dashboard.paper_dashboard
# Then open http://127.0.0.1:8787

# Vibe 5-minute point-in-time replay
.\.venv\Scripts\python.exe -m scripts.replay.vibe_replay_run_manager --start-date 2026-07-10 --end-date 2026-07-10 --symbols AAPL,MSFT,NVDA,SPY

# Performance report
.\.venv\Scripts\python.exe -m scripts.evaluation.generate_performance_report --root .

# Independent catalyst shadow metrics and API cost
.\.venv\Scripts\python.exe -m scripts.evaluation.evaluate_catalyst_strategy --root .

# Independent headline-drift event/firm-day/portfolio-day metrics
.\.venv\Scripts\python.exe -m scripts.evaluation.evaluate_news_drift --root .
```

## Promotion Gate

`config/evaluation.yaml` defaults to at least 20 forward sessions and 30 closed trades, positive net return, profit factor at least 1.2, drawdown no more than 10%, and zero rule violations. Historical replay and Vibe backtest results never satisfy the forward-evidence requirement by themselves.

Current external blockers are shown by `--readiness`. Until all required sources are ready, the service remains stopped or returns a failed-closed cycle.

## How a paper entry is decided

The equity candidate strategy is deterministic `weighted_relative_strength_v2`,
but its execution is currently `shadow_only` after negative net-of-cost forward
results. It records point-in-time candidates and 360-minute outcome labels but
does not create new equity entry orders.
Valid/fresh quotes, regular-session timing, fresh completed OHLCV, no existing
position, and the extreme-chase cap remain hard safety gates. Relative strength,
1-day and 5-day momentum, volume confirmation, market regime, and chase quality
contribute to one weighted score. Adaptive updates are disabled until aligned
360-minute labels show an out-of-sample edge after spread and slippage.

The baseline-screened DeepSeek comparison remains shadow-only: fast
non-thinking mode is used for structured news extraction, while thinking mode
is enabled only for its final Decision Manager. The dashboard displays
structured evidence and verdicts, never raw private chain-of-thought.

The independent catalyst lane remains shadow-only. The old executable AI-gated
lane is entry-frozen and retains only monitoring and exits. The new allocator
starts from a deterministic top-eight set rather than waiting for another
strategy to emit `buy`. DeepSeek emits one complete seven-bucket signed-return
distribution for a specified horizon. Python derives bullish, bearish, neutral,
and magnitude fields; raw values remain `uncalibrated` and never produce a
displayed probability EV. Overnight Challenge and Decision may use thinking;
fast stages do not. At 09:32 the system makes no model call: it reuses an active
conditional plan, refreshes quotes, reprices instruments, and reruns risk.
An after-hours or premarket signal must set `entry_now=false`; that field blocks
an order during research but does not cancel the saved conditional plan. Only a
plan created by an approved non-regular research stage can reach 09:32
revalidation, and the configured execution window closes at 09:37 ET. Newer
analysis for the same ticker supersedes the older plan, and
no-trade or failed revalidation invalidates it. Intraday proposals still require
`entry_now=true`.

The news-drift lane does not wait for a technical buy candidate. DeepSeek sees
only headline and source fields; ticker validation and all price, liquidity,
spread, latency, initial-reaction, and budget checks happen afterward in Python.
Its SQLite event ledger, snapshots, proposals, labels, reports, and scheduler
resource are isolated. At least 100 valid labels and 20 portfolio days are
required before profitability can be assessed, and it remains ineligible for
promotion while configured `shadow_only`. See
`references/llm_news_drift_policy.md`.

The options line is deterministic and independent. Its weighted v2 direction
model combines bullish/bearish technical evidence, market regime, and fresh
company-level catalyst evidence. A strong company-specific negative event or
clear relative weakness can support a long put even when SPY is neutral or
risk-on. Contract selection still requires 21-45 DTE, delta, spread, volume,
open interest, IV, Greeks, premium budget, and earnings-event checks, and now
rejects spreads above 2% while treating 1.5% as the preferred ceiling and recording exact rejection counts. One contract may be opened, premium risk is
capped at 3% of account equity, and aggregate option premium is capped at 8%.
Fills use bid/ask plus adverse slippage and can never violate the agent's limit.

Equity and options have separate orders, fills, positions, journals, win rates, and PnL. Inside each executable sleeve they share cash, a 60% total deployment cap, at most three total positions, at most three daily entries, and one executable exposure per underlying. Allocator equity positions also require a planned stop with at most 1% NAV planned loss, while the 25% single-stock limit remains a notional cap.
