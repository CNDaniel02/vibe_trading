# Options Paper Trading Policy

## Permitted v1 scope

- US equity and ordinary ETF options observed from Robinhood MCP.
- Buy-to-open one long call or one long put; sell-to-close only.
- 21-45 calendar DTE at entry, target absolute delta 0.45.
- Contract multiplier must be exactly 100.
- Real option bid/ask, quote timestamp, IV, delta, gamma, theta, vega, volume, open interest, expiration, and broker sellout time are persisted with the decision.

## Prohibited

- Sell-to-open, naked options, covered calls, cash-secured puts, spreads, multi-leg orders, margin, 0DTE, averaging down, and adding to a position.
- Synthetic fills from mark, midpoint, Black-Scholes, last trade, or underlying price.
- Automatic exercise, assignment, or equity delivery. Positions must be closed before configured DTE/sellout boundaries; a missing quote creates an incident and never a fabricated exit.
- Any Robinhood review, place, replace, or cancel call.

## Risk

Maximum loss for a permitted entry is premium plus configured costs. Default limits are one contract, 10% of account equity per option entry, 20% options-line deployment, 60% total equity/options deployment, and one open option position. Equity and options debit the same local cash account.

## Pricing and Greeks

Robinhood top-of-book controls paper fills and liquidation value. Robinhood Greeks are the primary live observations. `scripts/options/greeks.py` is an independent European Black-Scholes reasonableness reference only; US equity options are American-style, so it is not an exercise model or fill source.

## Historical replay limitation

The current replay engine has point-in-time underlying OHLCV but no licensed point-in-time US option chain with bid/ask, IV, Greeks, open interest, corporate-action adjustments, and delisted/expired contracts. Options historical replay remains disabled until such a dataset is configured. Forward options paper evaluation can run with Robinhood's current read-only observations.

Evaluated external choices:

- Alpaca historical options API: easiest future adapter because this project already has an Alpaca boundary; history starts in February 2024. Its free indicative feed is not actual OPRA quotes, so replay intended to validate fill realism should use the subscribed OPRA feed.
- Polygon options data: OPRA-derived historical quotes/trades/reference data and flat files. Full quote files are very large, so a ticker/date-scoped adapter or aggregate plan is preferable for this small account.
- Cboe DataShop Option Quote Intervals: official 1-minute/N-minute NBBO with optional IV and Greeks, available from 2012, but it is a purchased bulk dataset.

Do not silently mix indicative prices with OPRA/NBBO results. Every replay run must persist source, entitlement/feed, interval, adjustment policy, and data availability window.
