# Equity Decision Policy

Decision output is advisory until it passes risk checks and the paper broker accepts it.

Required fields:

- `decision_id`
- `symbol`
- `side`
- `order_type`
- `limit_price`
- `quantity`
- `decision_time`
- `quote_seen_at`
- `thesis`

Do not reuse a `decision_id` for a new logical order.
