# auto-trading-skill

Equity and long-premium options paper/shadow trading system. It observes real market data but routes every order to local virtual accounts. Live trading is not implemented.

Detailed Chinese documentation:

- [`PROJECT_ARCHITECTURE.md`](PROJECT_ARCHITECTURE.md): complete architecture, account isolation, runtime, equity/options/AI pipelines, state, evaluation, and known limitations.
- [`DEVELOPMENT_LOG.md`](DEVELOPMENT_LOG.md): append-only development and runtime repair history. Every behavior-changing update must add a new entry at the top.

The paper broker supports fractional equity quantities in increments of `0.001` shares. Position and order caps still apply before an order is created.

## Safety Boundary

- `paper: true`
- `live_readonly: false`
- `live_trading: false`
- `weighted_relative_strength_v2` is the active deterministic equity paper strategy.
- `long_directional_options_v2_weighted` is the active long-call/long-put paper strategy.
- `relative_strength_v1` and `long_directional_options_v1` remain unchanged deterministic shadow baselines.
- `multi_agent_relative_strength_v2_candidate` and Vibe Swarm are shadow/research only.
- `exa_deepseek_catalyst_v1` independently discovers candidates but remains shadow-only and creates no orders.
- `llm_news_drift_v1` discovers market-wide news before any technical screen and remains an isolated long-equity shadow experiment.
- `ai_gated_technical_v1` researches the deterministic technical top set and may trade only in its own isolated `$2,000` paper sleeve.
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
 equity paper path      options direction + contract filter
        |                         |
 equity risk gate          options risk gate
        |                         |
        +---- shared cash and total-risk cap ----+
        |                                        |
 equity paper broker                    options paper broker
        |                                        |
        +---- independent exits and metrics -----+
```

Screened equities also flow through the preserved shadow comparison. LLM output
never directly creates an order.

In parallel, `exa_deepseek_catalyst_v1` runs independently of baseline screening:

```text
core watchlist + market-wide earnings + saved read-only scans + Exa market events
        -> low-cost candidate extraction and structured ranking
        -> ticker instrument validation + timestamped evidence snapshot
        -> thinking Bull/News -> Challenge -> Decision
        -> deterministic equity or long-option risk veto
        -> shadow proposal and catalyst journal only
```

`ai_gated_technical_v1` is a separately measurable executable paper lane:

```text
read-only watchlist/scans/earnings -> deterministic technical top 5-8
        -> bounded parallel Exa evidence searches
        -> low-cost DeepSeek ranking
        -> News/Bull -> thinking Challenge -> thinking Decision
        -> deterministic equity/options/shared-risk veto
        -> isolated local paper sleeve, monitor, exit, journal, and metrics
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
LLM_MODEL=deepseek-v4-pro
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

# Credential and integration readiness
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
# --ai-gated-once, --ai-monitor-once, --news-drift-once, or --eod-once manually.

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

The active equity strategy is deterministic `weighted_relative_strength_v2`.
Valid/fresh quotes, regular-session timing, fresh completed OHLCV, no existing
position, and the extreme-chase cap remain hard safety gates. Relative strength,
1-day and 5-day momentum, volume confirmation, market regime, and chase quality
contribute to one weighted score. A weak soft feature no longer vetoes all other
evidence. Fixed weights are used for the first 100 valid matured one-hour observations;
after that an exponential minimum-squared-loss update can reweight features.
Every weight change is persisted and visible in the dashboard.

The baseline-screened DeepSeek comparison remains shadow-only: fast
non-thinking mode is used for structured news extraction, while thinking mode
is enabled for its Challenge Agent and Decision Manager. The dashboard displays
structured evidence and verdicts, never raw private chain-of-thought.

The independent catalyst lane remains shadow-only. The executable AI-gated
lane starts from the technical top set rather than waiting for an active buy
signal. Exa uses a 48-hour window, immutable evidence snapshots, URL/event/content
deduplication, primary-source verification for deep candidates, a two-hour ticker
cooldown, and a 24-hour event cooldown. DeepSeek ranks the bounded set cheaply;
thinking is enabled only for Challenge and final Decision. The AI sleeve has
independent cash, orders, positions, journals, and metrics so its return can be
compared without contaminating the deterministic account.

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
records exact rejection counts. One contract may be opened, premium risk is
capped at 10% of account equity, and the full options line is capped at 20%.
Fills use bid/ask plus adverse slippage and can never violate the agent's limit.

Equity and options have separate orders, fills, positions, journals, win rates, and PnL. They share cash, a 60% total deployed-risk cap, and a combined daily-entry cap. These defaults are intentionally conservative for a `$2,000` evaluation account.
