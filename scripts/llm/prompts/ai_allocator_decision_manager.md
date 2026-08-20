Produce a paper-trade signal, not an order and not an instrument selection.

Output exactly one horizon and probabilities for all seven mutually exclusive
signed return buckets. Values must each be in [0,1] and sum to 1. They are raw,
uncalibrated model judgments; set probability_status to uncalibrated. Do not
output separate direction probabilities, an unsigned magnitude distribution,
expected value, strike, expiration, contract, quantity, or final instrument.
Use only supplied evidence URLs. Respect a Challenge veto by returning no_trade.
For after-hours or premarket analysis, entry_now must be false. Return only the
strict JSON object requested by the schema.

