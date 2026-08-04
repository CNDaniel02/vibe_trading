# auto-trading-skill

Equity and long-premium options paper/shadow trading system. It observes real market data but routes every order to one shared internal `$2,000` virtual account. Live trading is not implemented.

The paper broker supports fractional equity quantities in increments of `0.001` shares. Position and order caps still apply before an order is created.

## Safety Boundary

- `paper: true`
- `live_readonly: false`
- `live_trading: false`
- Baseline `relative_strength_v1` may use the paper broker.
- `long_directional_options_v1` may independently buy one long call or long put in the options paper broker.
- `multi_agent_relative_strength_v2_candidate` and Vibe Swarm are shadow/research only.
- `exa_deepseek_catalyst_v1` independently discovers candidates but remains shadow-only and creates no orders.
- No adapter exposes create, submit, place, or cancel methods for a real broker.
- Options sell-to-open, short contracts, spreads, margin, 0DTE, exercise, and assignment are rejected.

## Architecture

```text
Vibe OHLCV + Robinhood MCP equity/options data + Exa news
                 |
        immutable timestamped snapshot
                 |
 deterministic regime/technical screening
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

Screened equities also flow through Exa -> News -> Challenge -> Decision,
then the deterministic risk veto and shadow journal. LLM output never creates an order.

In parallel, `exa_deepseek_catalyst_v1` runs independently of baseline screening:

```text
core watchlist + market-wide earnings + saved read-only scans + Exa market events
        -> low-cost candidate extraction and structured ranking
        -> ticker instrument validation + timestamped evidence snapshot
        -> thinking Bull/News -> Challenge -> Decision
        -> deterministic equity or long-option risk veto
        -> shadow proposal and catalyst journal only
```
```

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

The OAuth token and client registration are stored only in a current-user DPAPI-encrypted file under `state/`. The audit verifies the complete 50-tool manifest. Runtime calls use an explicit read-only allowlist for quotes, historicals, fundamentals, financials, technical indicators, earnings, saved scans, instrument search, and option market data. Scanner creation/update and all order tools remain unavailable.

Alpaca is enabled as a standby market-data source when `ALPACA_API_KEY_ID` and `ALPACA_API_SECRET_KEY` are present. Robinhood MCP remains the default quote provider; set `forward_data.quote_provider: alpaca` only when a workflow explicitly needs Alpaca historical bars, adjustment/corporate-action handling, or WebSocket streaming.

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

# One real Exa + DeepSeek discovery cycle; shadow proposals only
.\.venv\Scripts\python.exe -m scripts.orchestrator.forward_paper_service --catalyst-once

# Continuous APScheduler service
.\.venv\Scripts\python.exe -m scripts.orchestrator.forward_paper_service

# The continuous service prints one compact JSON status at startup and after
# each cycle. Press Ctrl+C to stop it gracefully and release its process lock.

# Read-only local GUI (run in a separate terminal while the service is running)
.\.venv\Scripts\python.exe -m scripts.dashboard.paper_dashboard
# Then open http://127.0.0.1:8787

# Vibe 5-minute point-in-time replay
.\.venv\Scripts\python.exe -m scripts.replay.vibe_replay_run_manager --start-date 2026-07-10 --end-date 2026-07-10 --symbols AAPL,MSFT,NVDA,SPY

# Performance report
.\.venv\Scripts\python.exe -m scripts.evaluation.generate_performance_report --root .

# Independent catalyst shadow metrics and API cost
.\.venv\Scripts\python.exe -m scripts.evaluation.evaluate_catalyst_strategy --root .
```

## Promotion Gate

`config/evaluation.yaml` defaults to at least 20 forward sessions and 30 closed trades, positive net return, profit factor at least 1.2, drawdown no more than 10%, and zero rule violations. Historical replay and Vibe backtest results never satisfy the forward-evidence requirement by themselves.

Current external blockers are shown by `--readiness`. Until all required sources are ready, the service remains stopped or returns a failed-closed cycle.

## How a paper entry is decided

The active equity strategy is deterministic `relative_strength_v1`: during regular NYSE hours it requires a valid quote, non-risk-off regime, 20-day relative strength of at least `0.25` percentage points, 5-day price change of at least `0.5%`, real intraday volume confirmation of at least `0.4`, and a bounded chase score. Up to three passing candidates are ranked by relative strength. It then applies the equity and shared-account paper risk caps.

DeepSeek remains shadow-only: fast non-thinking mode is used for structured news extraction, while thinking mode is enabled for the Challenge Agent and Decision Manager. The dashboard displays the resulting thesis, evidence, contrary evidence, challenge objections, and final deterministic risk verdict; it deliberately does not display raw private chain-of-thought.

The independent catalyst lane uses fast non-thinking calls for candidate extraction and ranking. Only the highest ranked, liquid, exactly resolved US-listed candidates proceed to thinking-enabled Bull/News, Challenge, and Decision stages. Exa evidence uses a 48-hour window, canonical URL/event/content deduplication, immutable timestamped snapshots, a two-hour ticker cooldown, and a 24-hour event cooldown. It may propose long equity, a long call, a long put, or no trade; the existing deterministic equity/options/shared-account risk engines retain final veto authority.

The options line is deterministic and independent. Its paper sampling profile requires at least `0.4` intraday volume confirmation, then considers 21-45 DTE long calls for confirmed strength and long puts for confirmed risk-off weakness. Contract selection still requires delta, spread, contract volume, open interest, IV, Greeks, and earnings-event checks. One contract may be opened, premium risk is capped at 10% of account equity, and the full options line is capped at 20%. Fills use the actual ask/bid plus adverse slippage; no theoretical midpoint can fill an order.

Equity and options have separate orders, fills, positions, journals, win rates, and PnL. They share cash, a 60% total deployed-risk cap, and a combined daily-entry cap. These defaults are intentionally conservative for a `$2,000` evaluation account.
