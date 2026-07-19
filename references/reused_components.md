# Reused Components And Design Sources

No third-party code is copied into this project in the first version.

Design ideas reused:

- Vibe-Trading: fail-closed pre-trade gate, append-only audit ledger, live-readonly versus live-write separation, mandate/runner safety model.
- claude-trading-skills: journal/postmortem workflow shape, drawdown circuit-breaker style risk gate, weekly-performance digest style metrics.
- TradingAgents: research -> debate/decision -> risk -> portfolio-manager pipeline concept.
- AI-Trader: signal/feed and heartbeat concepts for future agent coordination.

Implementation choice:

- Keep the first version small and deterministic.
- Use local JSON state and JSONL logs.
- Keep Vibe outside the project and invoke it through pinned, read-only adapters rather than vendoring its source.

## Vibe-Trading integration

Reference repo: sibling checkout `../references/repos/Vibe-Trading` at commit `6fc038d37f1767ae429bab435654b9b425ae66f4`.

Direct copies: none.

Adapter reuse:

- `vibe_market_data_adapter.py`: Vibe loader registry and Yahoo OHLCV normalization.
- `vibe_backtest_adapter.py`: isolated Vibe global-equity backtest artifacts.
- `vibe_research_swarm_adapter.py`: sanitized read-only research preset.
- `vibe_runtime.py` and `vibe_bridge.py`: pinned commit, clean-worktree, action, path, and tool checks.

Rejected:

- Vibe Shadow Account as a paper broker.
- Vibe's current Robinhood write path.
- Unstructured investment-committee output as an executable decision.
- Vibe's custom live runner for market-hours enforcement.
- Wholesale copying of the Vibe dependency graph or source tree.
## ai-berkshire review

Reference repo: sibling checkout `../references/repos/ai-berkshire` at commit `afd7393`.

Checked files:

- `codex-skills/investment-team/SKILL.md`
- `skills/investment-team.md`
- `skills/news-pulse.md`
- `skills/earnings-team.md`
- `skills/thesis-tracker.md`
- `AGENTS.md`
- `tools/financial_rigor.py`
- `tools/report_audit.py`
- `tools/momentum_backtest.py`
- `tools/momentum_backtest_v2.py`

Direct copies: none. The repo stays in a separate sibling reference-repository directory; this project reuses workflow patterns and keeps attribution here instead of scattering copied code.

Refactored into this project:

- Team Lead plus analyst task structure.
- Evidence/source gap/bull-bear/challenge-review schema.
- Availability rating and source gap disclosure.
- Thesis, assumptions, red lines, and invalidation triggers.
- Report audit concept and exact-calculation discipline as policy.
- Momentum trigger idea for future strategy work, not the original script implementation.

Rejected for v1:

- Claude-specific Task tooling and slash-command surfaces.
- Publication/report generation workflow.
- Direct use of mojibake-heavy report audit regexes.
- Direct use of momentum backtest scripts because they mix hard-coded fundamentals, print-side effects, and a separate backtest/fill path.
