from __future__ import annotations

from scripts.options.models import OptionContract, OptionQuote
from scripts.options.risk_gate import validate_option_quote


def rank_contracts(
    contracts: list[OptionContract],
    quotes: dict[str, OptionQuote],
    now: str,
    config: dict,
) -> list[tuple[OptionContract, OptionQuote]]:
    target_delta = float(config["options_universe"].get("target_abs_delta", 0.45))
    accepted: list[tuple[OptionContract, OptionQuote]] = []
    for contract in contracts:
        quote = quotes.get(contract.option_id)
        if not validate_option_quote(quote, now, config).approved:
            continue
        assert quote is not None
        delta = abs(float(quote.delta or 0))
        universe = config["options_universe"]
        if not float(universe.get("min_abs_delta", 0)) <= delta <= float(universe.get("max_abs_delta", 1)):
            continue
        accepted.append((contract, quote))
    accepted.sort(
        key=lambda item: (
            abs(abs(float(item[1].delta or 0)) - target_delta),
            item[1].spread_pct(),
            -int(item[1].open_interest or 0),
            -int(item[1].volume or 0),
        )
    )
    return accepted
