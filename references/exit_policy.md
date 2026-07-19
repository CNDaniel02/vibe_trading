# Exit Policy

The first version supports deterministic exit checks:

- Stop loss if configured by the decision.
- Take profit if configured by the decision.
- Time stop if configured by the decision.
- End-of-day flatten: produce an exit signal before regular close by `exit_before_close_minutes`.

Exit orders still pass through risk and paper execution. A requested exit can remain open if the limit is not reachable.
