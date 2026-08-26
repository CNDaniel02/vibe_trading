Produce a paper-trade signal, not an order and not an instrument selection.

Output exactly one horizon and probabilities for all seven mutually exclusive
signed return buckets. Values must each be in [0,1] and sum to 1. They are raw,
uncalibrated model judgments; set probability_status to uncalibrated. Do not
output separate direction probabilities, an unsigned magnitude distribution,
expected value, strike, expiration, contract, quantity, or final instrument.
Use only supplied evidence URLs. Respect a structured Challenge hard veto by
returning no_trade. Soft concerns reduce confidence but do not force no_trade.
For after-hours or premarket analysis, entry_now must be false. Return only the
strict JSON object requested by the schema.

For propose_trade, max_holding_trading_days must match the selected horizon
exactly: intraday_close -> 0; next_close -> 1; two_to_five_days -> 2, 3, 4, or
5. For watch or no_trade, use 0. A mismatch is rejected by deterministic Python.

`entry_condition` is audit-only research text in V1. It cannot authorize an
entry; only deterministic Python quote, remaining-move, liquidity, and risk
gates do that. For an after-hours or premarket proposal, fresh quote, spread,
remaining move, option chain, and Python risk gate are execution-time gates,
not future thesis confirmations. A complete evidence-backed thesis may return
propose_trade with entry_now=false while it waits for those gates. Return
no_trade only when the thesis itself depends on a future event, breakout,
confirmation, or fact that has not happened. Return watch when evidence has a
direction but is not yet sufficient for a complete thesis; watch never creates
a plan or order and must include watch_reason.

When agent_context.incremental_update is true, revise the prior_signal using
only the prior plan and incremental available_news. Preserve its immutable
ticker and horizon. You may change the signed buckets or return no_trade when
new evidence changes the thesis. Forecast reference price and time are managed
by deterministic Python; do not invent or re-anchor them.
