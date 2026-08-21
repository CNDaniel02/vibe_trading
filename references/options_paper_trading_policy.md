# Options Paper Trading Policy

## Permitted paper scope

- US equity and ordinary ETF options observed from Robinhood MCP.
- Buy-to-open one long call or one long put; sell-to-close only.
- 21-45 calendar DTE at entry, target absolute delta 0.45.
- Contract multiplier must be exactly 100.
- Real option bid/ask, quote timestamp, IV, delta, gamma, theta, vega, volume, open interest, expiration, and broker sellout time are persisted with the decision.
- Direction is weighted. A company-specific negative event or strong individual
  relative weakness may support a long put without requiring SPY to be risk-off.

## Prohibited

- Sell-to-open, naked options, covered calls, cash-secured puts, spreads, multi-leg orders, margin, 0DTE, averaging down, and adding to a position.
- Synthetic fills from mark, midpoint, Black-Scholes, last trade, or underlying price.
- Automatic exercise, assignment, or equity delivery. Positions must be closed before configured DTE/sellout boundaries; a missing quote creates an incident and never a fabricated exit.
- Any Robinhood review, place, replace, or cancel call.

## Risk

Maximum loss for a permitted entry is premium plus configured costs. Default
limits are one contract, 3% of account equity per option entry, 8% aggregate
option premium, 60% total equity/options deployment, three total executable
positions, three daily entries, and one exposure per underlying across equity
and options. Equity and options debit the same local cash account inside each
strategy account. Legacy sleeves remain separate; the new allocator uses an
isolated `$10,000` sleeve.

## Pricing and Greeks

Robinhood top-of-book controls paper fills and liquidation value. Robinhood Greeks are the primary live observations. For instrument comparison, `scripts/options/scenario_pricing.py` anchors to the observed midpoint and reprices underlying-move, remaining-time, and IV contraction/unchanged/expansion scenarios. Delta, Gamma, Theta, and Vega are sensitivity diagnostics. The scenario model is not a fill source, exercise model, or claim of exact American-option valuation.

## Historical replay limitation

The current replay engine has point-in-time underlying OHLCV but no licensed point-in-time US option chain with bid/ask, IV, Greeks, open interest, corporate-action adjustments, and delisted/expired contracts. Options historical replay remains disabled until such a dataset is configured. Forward options paper evaluation can run with Robinhood's current read-only observations.

Evaluated external choices:

- Alpaca historical options API: easiest future adapter because this project already has an Alpaca boundary; history starts in February 2024. Its free indicative feed is not actual OPRA quotes, so replay intended to validate fill realism should use the subscribed OPRA feed.
- Polygon options data: OPRA-derived historical quotes/trades/reference data and flat files. Full quote files are very large, so a ticker/date-scoped adapter or aggregate plan is preferable for this small account.
- Cboe DataShop Option Quote Intervals: official 1-minute/N-minute NBBO with optional IV and Greeks, available from 2012, but it is a purchased bulk dataset.

Do not silently mix indicative prices with OPRA/NBBO results. Every replay run must persist source, entitlement/feed, interval, adjustment policy, and data availability window.
