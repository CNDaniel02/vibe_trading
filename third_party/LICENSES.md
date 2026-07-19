# Third-Party Reuse Notes

This first paper/shadow trading implementation does not vendor third-party source code.

Reference projects inspected locally:

- TradingAgents: Apache-2.0. Used as architecture reference for multi-agent research and reflection flow.
- claude-trading-skills: MIT. Used as architecture reference for skill/workflow layout, trade memory, and performance-review patterns.
- Vibe-Trading: MIT, pinned at `6fc038d37f1767ae429bab435654b9b425ae66f4`. No Vibe source is copied into this project. Its loaders, backtest engine, and optional Swarm runtime are invoked through a subprocess adapter; the original checkout and license remain in a separate sibling reference-repository directory.
- AI-Trader: README advertises MIT, but no local LICENSE file was present in the shallow clone. Used only as conceptual reference for signals and heartbeat-style participation.
