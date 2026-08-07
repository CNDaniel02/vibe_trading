# Simulated Execution Policy

Use bid/ask, not midpoint.

Buy limit:

- Compute `current_ask + adverse_slippage`.
- If that adverse price is greater than the limit, keep the order open.
- Otherwise fill at the adverse price. A buy fill never exceeds its limit.

Sell limit:

- Compute `current_bid - adverse_slippage`.
- If that adverse price is lower than the limit, keep the order open.
- Otherwise fill at the adverse price. A sell fill never falls below its limit.

Market orders:

- Buy uses ask plus adverse slippage.
- Sell uses bid minus adverse slippage.

Supported states:

`created`, `submitted_to_paper_broker`, `open`, `partially_filled`, `filled`, `cancelled`, `expired`, `rejected`.
