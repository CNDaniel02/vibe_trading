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

## Fill crash consistency

Every successful equity or option fill is first recorded as a `prepared`
transaction in `paper_fill_transactions.json`, keyed by `fill_id`. The WAL
contains the exact post-fill account, position, terminal order, daily counter,
and lifecycle-journal state plus the fill log records that must be written.

Only after the WAL exists may the broker replace those state files. A broker
restart replays every `prepared` transaction to the same target snapshots and
then marks it `committed`. It never reruns cash arithmetic or counter
increments. JSONL records carry `fill_transaction_id` and are appended at most
once, so interruption after any individual state or log write is idempotent.

Each strategy sleeve has its own WAL under its namespaced state directory. A
committed record retains the fill and affected file list for audit, while its
temporary snapshots are removed to limit ledger growth.
