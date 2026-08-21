# Data Source Policy

## Automated Runtime Sources

- Robinhood Trading MCP: explicit read-only methods provide equity bid/ask, regular-session 5-minute volume bars, option chains, option instruments, option bid/ask and Greeks, and the earnings calendar. The project never obtains an MCP credential from Codex; its separately authorized OAuth token and client-registration metadata live in a DPAPI-encrypted file under ignored `state/`.
- Alpaca Market Data REST is retained for broad or long historical bar retrieval, explicit adjustment and corporate-action handling, and WebSocket data. The optional snapshot adapter reads `ALPACA_API_KEY_ID` and `ALPACA_API_SECRET_KEY` only. The default feed is `iex`; do not label it consolidated SIP data.
- Exa Search REST: ticker-scoped and bounded market-wide recent news with publication timestamps, URLs, and inline `contents.highlights`. Credentials are read from `EXA_API_KEY` only. News remains evidence for the agents and cannot directly create an order.
- The catalyst lane also uses a bounded market-level Exa search. Exa never replaces structured quotes, historical bars, fundamentals, earnings, liquidity, or tradability checks. URL, event fingerprint, and content hash deduplication occurs before model use.
- The news-drift lane rotates one broad market query every minute and sends only unseen headline/source fields to its price-blind LLM classifier. Exact ticker, quotes, fundamentals, liquidity, spread, and initial reaction are checked afterward. Deep Search, Exa Agent, Monitors, and a separate Contents request are not currently used; inline highlights are already returned by Search.
- Robinhood MCP saved scanners are read-only discovery inputs. The service may list and run existing scans but never creates or modifies a scanner.
- Vibe-Trading at the pinned commit in `config/integrations.yaml`: Yahoo-backed OHLCV, independent backtests, and an optional read-only research Swarm.
- Robinhood project scope is strictly read-only. The general discovery/market-data client allowlist is `get_earnings_calendar`, `get_earnings_results`, `get_equity_fundamentals`, `get_equity_historicals`, `get_equity_quotes`, `get_equity_technical_indicators`, `get_equity_tradability`, `get_financials`, `get_scans`, `run_scan`, and instrument `search`. A separate narrowly scoped option adapter reads chains, instruments, quotes, and the earnings calendar. Account, portfolio, position, order, watchlist mutation, scanner mutation, review, placement, and cancellation tools have no project runtime method.

Official references:

- Alpaca latest snapshots: https://docs.alpaca.markets/us/reference/stocksnapshots-1
- Alpaca market-data authentication: https://docs.alpaca.markets/us/docs/about-market-data-api
- Exa search: https://exa.ai/docs/reference/search
- Alpaca historical options: https://docs.alpaca.markets/us/docs/historical-option-data
- Polygon options overview: https://polygon.io/docs/options/getting-started
- Cboe Option Quote Intervals: https://datashop.cboe.com/option-quote-intervals

## Codex Plugins

Alpaca, Public Equity Investing, Investment Banking, Binance, and Exa MCP can assist an interactive Codex task. A standalone APScheduler Python process cannot assume that Codex plugin/MCP tools are callable. Continuous operation therefore uses explicit read-only HTTP adapters, the project-owned OAuth MCP quote client, or the pinned local Vibe process.

Binance is excluded because this mandate permits only US equities and ordinary non-levered ETFs. Investment Banking is a workflow aid, not a top-of-book quote feed.

## Fail-Closed Rules

- Missing credentials, missing bid/ask, stale/future timestamps, partial symbol responses, abnormal spread, or unavailable Vibe commit stop the affected cycle.
- Do not substitute midpoint for bid/ask.
- Do not use Exa's crawl time as publication time.
- Do not use current web news in historical replay.
- Intraday replay labels its top-of-book as synthetic and applies configured adverse spread and slippage.
