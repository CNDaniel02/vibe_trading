from __future__ import annotations

from typing import Any, Iterable

from scripts.options.greeks import black_scholes_estimate
from scripts.options.models import OptionContract, OptionQuote
from scripts.options.tick_rounding import (
    option_price_tick,
    round_down_to_tick,
    round_up_to_tick,
)


def _adverse_slippage(price: float, costs: dict[str, Any]) -> float:
    return max(
        price * float(costs.get("slippage_bps", 0)) / 10_000,
        float(costs.get("minimum_slippage_usd_per_contract", 0)),
    )


def reprice_option_scenarios(
    contract: OptionContract,
    quote: OptionQuote,
    *,
    spot: float,
    now: str,
    elapsed_calendar_days: float,
    move_pct: float,
    iv_shifts: Iterable[float],
    costs: dict[str, Any],
) -> dict[str, Any]:
    if spot <= 0 or quote.mid <= 0:
        raise ValueError("spot and option midpoint must be positive")
    if quote.implied_volatility is None or quote.implied_volatility <= 0:
        raise ValueError("positive implied volatility is required")
    dte = contract.dte(now)
    if dte <= 0 or elapsed_calendar_days < 0 or elapsed_calendar_days >= dte:
        raise ValueError("scenario horizon must remain before option expiration")
    initial_years = dte / 365.0
    remaining_years = (dte - elapsed_calendar_days) / 365.0
    base_iv = float(quote.implied_volatility)
    initial_model = black_scholes_estimate(
        option_type=contract.option_type,
        spot=spot,
        strike=contract.strike_price,
        years_to_expiry=initial_years,
        volatility=base_iv,
    )
    scenario_spot = spot * (1 + move_pct / 100)
    raw_entry = quote.ask + _adverse_slippage(quote.ask, costs)
    entry_price = round_up_to_tick(
        raw_entry,
        option_price_tick(contract, raw_entry, costs),
    )
    spread = max(0.0, quote.ask - quote.bid)
    commission = float(costs.get("commission_per_contract_usd", 0))
    scenarios: list[dict[str, Any]] = []
    for raw_shift in iv_shifts:
        shift = float(raw_shift)
        scenario_iv = max(0.01, base_iv + shift)
        estimate = black_scholes_estimate(
            option_type=contract.option_type,
            spot=scenario_spot,
            strike=contract.strike_price,
            years_to_expiry=remaining_years,
            volatility=scenario_iv,
        )
        intrinsic = (
            max(0.0, scenario_spot - contract.strike_price)
            if contract.option_type == "call"
            else max(0.0, contract.strike_price - scenario_spot)
        )
        anchored_mid = max(
            intrinsic,
            0.0,
            quote.mid + estimate.price - initial_model.price,
        )
        projected_bid = max(0.0, anchored_mid - spread / 2)
        raw_exit = max(
            0.0,
            projected_bid - _adverse_slippage(projected_bid, costs),
        )
        executable_bid = max(
            0.0,
            round_down_to_tick(
                raw_exit,
                option_price_tick(contract, raw_exit, costs),
            ),
        )
        net_pnl = (
            (executable_bid - entry_price) * contract.multiplier
            - commission * 2
        )
        scenarios.append(
            {
                "iv_shift": shift,
                "scenario_iv": round(scenario_iv, 6),
                "scenario_spot": round(scenario_spot, 6),
                "repriced_mid": round(anchored_mid, 6),
                "executable_exit_bid": round(executable_bid, 6),
                "net_pnl_usd": round(net_pnl, 6),
                "scenario_net_return_pct": round(
                    net_pnl / (entry_price * contract.multiplier),
                    8,
                ),
            }
        )
    if not scenarios:
        raise ValueError("at least one IV scenario is required")
    conservative = min(scenarios, key=lambda item: item["executable_exit_bid"])
    return {
        "method": "midpoint_anchored_black_scholes_repricing",
        "option_id": contract.option_id,
        "option_type": contract.option_type,
        "spot": spot,
        "move_pct": move_pct,
        "elapsed_calendar_days": elapsed_calendar_days,
        "entry_executable_ask": round(entry_price, 6),
        "conservative_exit_bid": conservative["executable_exit_bid"],
        "conservative_net_pnl_usd": conservative["net_pnl_usd"],
        "conservative_net_return_pct": conservative[
            "scenario_net_return_pct"
        ],
        "scenarios": scenarios,
        "greeks": {
            "delta": quote.delta,
            "gamma": quote.gamma,
            "theta": quote.theta,
            "vega": quote.vega,
        },
        "probability_ev_available": False,
        "probability_ev_usd": None,
    }
