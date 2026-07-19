# Simulated Execution Policy

Use bid/ask, not midpoint.

Buy limit:

- If current ask is greater than limit price, keep the order open.
- Otherwise fill at `max(limit_price, current_ask) + adverse_slippage`.

Sell limit:

- If current bid is lower than limit price, keep the order open.
- Otherwise fill at `min(limit_price, current_bid) - adverse_slippage`.

Market orders:

- Buy uses ask plus adverse slippage.
- Sell uses bid minus adverse slippage.

Supported states:

`created`, `submitted_to_paper_broker`, `open`, `partially_filled`, `filled`, `cancelled`, `expired`, `rejected`.
