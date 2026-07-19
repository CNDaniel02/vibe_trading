# auto-trading-skill

Equity-only paper and shadow trading system. It observes real market data but routes every order to an internal `$2,000` virtual account. Live trading is not implemented.

## Safety Boundary

- `paper: true`
- `live_readonly: false`
- `live_trading: false`
- Baseline `relative_strength_v1` may use the paper broker.
- `multi_agent_relative_strength_v2_candidate` and Vibe Swarm are shadow/research only.
- No adapter exposes create, submit, place, or cancel methods for a real broker.

## Architecture

```text
Vibe OHLCV + Alpaca bid/ask + Exa news
                 |
        immutable timestamped snapshot
                 |
 deterministic regime/technical screening
        |                         |
 baseline paper path       News -> Challenge -> Decision
        |                         |
 deterministic risk gate   deterministic risk veto
        |                         |
 internal paper broker        shadow journal only
        |
 monitor -> deterministic exit -> metrics
```

Vibe is pinned at `6fc038d37f1767ae429bab435654b9b425ae66f4`. Its source is not copied; an isolated subprocess adapter provides OHLCV, independent backtests, and optional read-only Swarm research.

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
ALPACA_API_KEY_ID
ALPACA_API_SECRET_KEY
EXA_API_KEY
```

For API-driven News, Challenge, and Decision agents also set:

```text
OPENAI_API_KEY
LLM_MODEL
```

Then enable Alpaca and Exa in `config/integrations.yaml`. Change `config/llm.yaml` from `provider: mock` to `provider: api` only after the API eval passes.

## Commands

```powershell
# All tests
.\.venv\Scripts\python.exe -m pytest -q

# No-network end-to-end fixture dry run
.\.venv\Scripts\python.exe -m scripts.orchestrator.dry_run_forward_pipeline

# Credential and integration readiness
.\.venv\Scripts\python.exe -m scripts.orchestrator.forward_paper_service --readiness

# One real forward paper cycle; fails closed outside regular NYSE hours
.\.venv\Scripts\python.exe -m scripts.orchestrator.forward_paper_service --once

# Continuous APScheduler service
.\.venv\Scripts\python.exe -m scripts.orchestrator.forward_paper_service

# Vibe 5-minute point-in-time replay
.\.venv\Scripts\python.exe -m scripts.replay.vibe_replay_run_manager --start-date 2026-07-10 --end-date 2026-07-10 --symbols AAPL,MSFT,NVDA,SPY

# Performance report
.\.venv\Scripts\python.exe -m scripts.evaluation.generate_performance_report --root .
```

## Promotion Gate

`config/evaluation.yaml` defaults to at least 20 forward sessions and 30 closed trades, positive net return, profit factor at least 1.2, drawdown no more than 10%, and zero rule violations. Historical replay and Vibe backtest results never satisfy the forward-evidence requirement by themselves.

Current external blockers are shown by `--readiness`. Until all required sources are ready, the service remains stopped or returns a failed-closed cycle.
