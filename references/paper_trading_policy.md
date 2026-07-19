# Paper Trading Policy

Paper mode is the default and only implemented trading mode in this version.

Paper mode may read real market data and Robinhood account state through read-only adapters, but every order must route to the local paper broker. Paper cash, positions, orders, and fills live under this skill's `state/` and `logs/` directories and never modify broker state.

Forbidden in paper mode:

- Calling live order placement tools.
- Treating a submitted or open paper order as a filled position.
- Using Robinhood cash as paper buying power.
- Filling an order without a timestamped quote.
