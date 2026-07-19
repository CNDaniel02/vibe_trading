# Data Source Policy

## Automated Runtime Sources

- Alpaca Market Data REST: latest multi-symbol snapshots with bid, ask, trade, session volume, and previous close. Credentials are read from `ALPACA_API_KEY_ID` and `ALPACA_API_SECRET_KEY` only. The default feed is `iex`; do not label it consolidated SIP data.
- Exa Search REST: ticker-scoped recent news with publication timestamps, URLs, and highlights. Credentials are read from `EXA_API_KEY` only. News remains evidence for the shadow agents and never creates an order.
- Vibe-Trading at the pinned commit in `config/integrations.yaml`: Yahoo-backed OHLCV, independent backtests, and an optional read-only research Swarm.
- Robinhood: read-only account inspection only. It is not a forward quote dependency and no write tool is registered in this project.

Official references:

- Alpaca latest snapshots: https://docs.alpaca.markets/us/reference/stocksnapshots-1
- Alpaca market-data authentication: https://docs.alpaca.markets/us/docs/about-market-data-api
- Exa search: https://exa.ai/docs/reference/search

## Codex Plugins

Alpaca, Public Equity Investing, Investment Banking, Binance, and Exa MCP can assist an interactive Codex task. A standalone APScheduler Python process cannot assume that Codex plugin/MCP tools are callable. Continuous operation therefore uses explicit read-only HTTP adapters or the pinned local Vibe process.

Binance is excluded because this mandate permits only US equities and ordinary non-levered ETFs. Investment Banking is a workflow aid, not a top-of-book quote feed.

## Fail-Closed Rules

- Missing credentials, missing bid/ask, stale/future timestamps, partial symbol responses, abnormal spread, or unavailable Vibe commit stop the affected cycle.
- Do not substitute midpoint for bid/ask.
- Do not use Exa's crawl time as publication time.
- Do not use current web news in historical replay.
- Intraday replay labels its top-of-book as synthetic and applies configured adverse spread and slippage.
