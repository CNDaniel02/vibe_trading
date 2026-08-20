from __future__ import annotations

import math
from typing import Any
from uuid import uuid4

from scripts.core.models import Quote
from scripts.decision.signed_return_signal import derive_signal_summary
from scripts.options.models import OptionContract, OptionQuote
from scripts.options.scenario_pricing import reprice_option_scenarios


def _slippage(price: float, config: dict[str, Any], *, option: bool) -> float:
    minimum_key = (
        "minimum_slippage_usd_per_contract"
        if option
        else "minimum_slippage_usd"
    )
    return max(
        price * float(config.get("slippage_bps", 0)) / 10_000,
        float(config.get(minimum_key, 0)),
    )


def _fractional_floor(value: float, increment: float) -> float:
    return math.floor((value + 1e-12) / increment) * increment


def _horizon_days(signal: dict[str, Any]) -> float:
    horizon = signal["horizon"]
    if horizon == "intraday_close":
        return 0.25
    if horizon == "next_close":
        return 1.0
    return float(max(2, min(5, int(signal.get("max_holding_trading_days", 3)))))


def _equity_candidate(
    quote: Quote,
    move_pct: float,
    account_state: dict[str, Any],
    config: dict[str, Any],
    profile: dict[str, Any],
) -> dict[str, Any]:
    costs = config.get("costs", {})
    entry = quote.ask + _slippage(quote.ask, costs, option=False)
    scenario_mid = quote.last * (1 + move_pct / 100)
    projected_bid = max(0.0, scenario_mid - (quote.ask - quote.bid) / 2)
    exit_price = max(
        0.0,
        projected_bid - _slippage(projected_bid, costs, option=False),
    )
    net_return = exit_price / entry - 1 if entry > 0 else float("-inf")
    nav = float(account_state["nav_usd"])
    cash = float(account_state["cash_usd"])
    total_deployed = float(account_state.get("equity_deployed_usd", 0)) + float(
        account_state.get("options_deployed_usd", 0)
    )
    capacity = min(
        cash,
        nav * 0.25,
        max(0.0, nav * 0.50 - float(account_state.get("equity_deployed_usd", 0))),
        max(0.0, nav * 0.60 - total_deployed),
    )
    stop_price = entry * (1 - float(profile.get("equity_planned_stop_pct", 0.03)))
    unit_stop_risk = max(0.0, entry - stop_price)
    if unit_stop_risk > 0:
        capacity_quantity = min(capacity / entry, nav * 0.01 / unit_stop_risk)
    else:
        capacity_quantity = 0.0
    increment = float(config.get("risk", {}).get("fractional_share_increment", 0.001))
    quantity = _fractional_floor(capacity_quantity, increment)
    return {
        "instrument_type": "equity",
        "ticker": quote.symbol,
        "entry_price": round(entry, 6),
        "scenario_exit_price": round(exit_price, 6),
        "conservative_move_pct": move_pct,
        "conservative_net_return_pct": round(net_return, 8),
        "break_even_move_pct": round((entry / quote.last - 1) * 100, 6),
        "planned_stop_price": round(stop_price, 6),
        "quantity": round(quantity, 6),
        "notional_usd": round(quantity * entry, 6),
        "risk_usd": round(quantity * unit_stop_risk, 6),
        "eligible": quantity > 0
        and net_return
        >= float(profile.get("equity_minimum_scenario_return_pct", 0)),
        "reason": (
            None
            if quantity > 0
            and net_return
            >= float(profile.get("equity_minimum_scenario_return_pct", 0))
            else "equity conservative scenario does not clear risk and return hurdles"
        ),
    }


def _option_candidate(
    contract: OptionContract,
    quote: OptionQuote,
    *,
    signal: dict[str, Any],
    spot: float,
    move_pct: float,
    account_state: dict[str, Any],
    config: dict[str, Any],
    profile: dict[str, Any],
    now: str,
) -> dict[str, Any]:
    iv_shifts = [
        float(value)
        for value in profile.get("option_iv_shifts", [-0.05, 0.0, 0.05])
    ]
    repricing = reprice_option_scenarios(
        contract,
        quote,
        spot=spot,
        now=now,
        horizon_days=_horizon_days(signal),
        move_pct=move_pct,
        iv_shifts=iv_shifts,
        costs=config.get("options_costs", {}),
    )
    entry_price = float(repricing["entry_executable_ask"])
    unit_risk = entry_price * contract.multiplier
    nav = float(account_state["nav_usd"])
    cash = float(account_state["cash_usd"])
    total_deployed = float(account_state.get("equity_deployed_usd", 0)) + float(
        account_state.get("options_deployed_usd", 0)
    )
    capacity = min(
        cash,
        nav * 0.03,
        max(0.0, nav * 0.08 - float(account_state.get("options_deployed_usd", 0))),
        max(0.0, nav * 0.60 - total_deployed),
    )
    quantity = min(1, math.floor((capacity + 1e-9) / unit_risk)) if unit_risk > 0 else 0
    preferred = quote.spread_pct() <= float(
        config.get("options_universe", {}).get("preferred_max_spread_pct", 0.015)
    )
    hurdle = float(profile.get("option_minimum_scenario_return_pct", 0.08))
    if not preferred:
        hurdle *= float(profile.get("non_preferred_spread_hurdle_multiplier", 1.5))
    scenario_return = float(repricing["conservative_net_return_pct"])
    eligible = quantity > 0 and scenario_return >= hurdle
    break_even_move = _option_break_even_move(
        contract,
        quote,
        spot=spot,
        now=now,
        horizon_days=_horizon_days(signal),
        conservative_iv_shift=min(iv_shifts),
        costs=config.get("options_costs", {}),
    )
    return {
        "instrument_type": contract.option_type,
        "ticker": contract.underlying,
        "option_id": contract.option_id,
        "expiration_date": contract.expiration_date,
        "strike_price": contract.strike_price,
        "multiplier": contract.multiplier,
        "entry_price": entry_price,
        "quantity": quantity,
        "risk_usd": round(unit_risk * quantity, 6),
        "spread_pct": round(quote.spread_pct(), 8),
        "preferred_spread": preferred,
        "conservative_move_pct": move_pct,
        "conservative_net_return_pct": scenario_return,
        "break_even_move_pct": break_even_move,
        "scenario_repricing": repricing,
        "eligible": eligible,
        "reason": (
            None
            if eligible
            else (
                "same option contract exceeds deterministic premium capacity"
                if quantity <= 0
                else "option conservative repricing does not clear return hurdle"
            )
        ),
    }


def _option_break_even_move(
    contract: OptionContract,
    quote: OptionQuote,
    *,
    spot: float,
    now: str,
    horizon_days: float,
    conservative_iv_shift: float,
    costs: dict[str, Any],
) -> float | None:
    sign = 1 if contract.option_type == "call" else -1
    for step in range(0, 81):
        move = sign * step * 0.25
        value = reprice_option_scenarios(
            contract,
            quote,
            spot=spot,
            now=now,
            horizon_days=horizon_days,
            move_pct=move,
            iv_shifts=[conservative_iv_shift],
            costs=costs,
        )
        if float(value["conservative_net_return_pct"]) >= 0:
            return move
    return None


def build_short_equity_counterfactual(
    signal: dict[str, Any],
    quote: Quote,
    costs: dict[str, Any],
) -> dict[str, Any] | None:
    summary = derive_signal_summary(signal)
    if summary["direction"] != "bearish":
        return None
    entry = max(0.0, quote.bid - _slippage(quote.bid, costs, option=False))
    scenario_mid = quote.last * (1 + float(summary["conservative_move_pct"]) / 100)
    cover = scenario_mid + (quote.ask - quote.bid) / 2
    cover += _slippage(cover, costs, option=False)
    return {
        "benchmark_name": "short_equity_counterfactual",
        "ticker": quote.symbol,
        "entry_price": round(entry, 6),
        "scenario_cover_price": round(cover, 6),
        "scenario_net_return_pct": round((entry - cover) / entry, 8) if entry else None,
        "probability_status": "uncalibrated",
        "creates_order": False,
        "enters_account": False,
        "merged_with_long_put_pnl": False,
    }


def build_same_instrument_counterfactual(
    allocation: dict[str, Any],
    *,
    nav_usd: float = 2_000,
) -> dict[str, Any]:
    selected = dict(allocation["selected_instrument"])
    instrument_type = str(selected["instrument_type"])
    entry_price = float(selected["entry_price"])
    if instrument_type == "equity":
        unit_risk = max(
            0.0,
            entry_price - float(selected.get("planned_stop_price", entry_price)),
        )
        max_quantity = min(
            nav_usd * 0.25 / entry_price,
            nav_usd * 0.01 / unit_risk if unit_risk > 0 else 0,
        )
        max_quantity = _fractional_floor(max_quantity, 0.001)
        risk_usd = float(selected.get("risk_usd", 0))
        affordable = max_quantity > 0
        reason = None if affordable else "same equity instrument has no deterministic capacity"
    else:
        unit_risk = entry_price * float(selected.get("multiplier", 100))
        max_quantity = min(1, math.floor((nav_usd * 0.03 + 1e-9) / unit_risk))
        risk_usd = unit_risk * float(selected.get("quantity", 1))
        affordable = max_quantity >= int(selected.get("quantity", 1))
        reason = (
            None
            if affordable
            else "same option contract exceeds 3% per-entry premium risk"
        )
    identity = {
        key: selected[key]
        for key in (
            "instrument_type",
            "ticker",
            "option_id",
            "expiration_date",
            "strike_price",
        )
        if key in selected
    }
    return {
        "source_allocation_id": allocation["allocation_id"],
        "counterfactual_nav_usd": nav_usd,
        "instrument_identity": identity,
        "affordable": affordable,
        "max_affordable_quantity": max_quantity,
        "proposed_risk_usd": round(risk_usd, 6),
        "risk_pct_of_nav": round(risk_usd / nav_usd, 8),
        "rejection_reason": reason,
        "alternative_instrument_considered": False,
        "creates_order": False,
    }


def allocate_instrument(
    signal: dict[str, Any],
    underlying_quote: Quote,
    option_candidates: list[tuple[OptionContract, OptionQuote]],
    account_state: dict[str, Any],
    config: dict[str, Any],
    now: str,
) -> dict[str, Any]:
    profile = config.get("strategies", {}).get("ai_instrument_allocator_v1", {})
    summary = derive_signal_summary(signal)
    masses = sorted(
        (
            float(summary["bullish_probability"]),
            float(summary["bearish_probability"]),
            float(summary["neutral_probability"]),
        ),
        reverse=True,
    )
    direction = str(summary["direction"])
    allocation_id = f"aia_{uuid4().hex}"
    short_benchmark = build_short_equity_counterfactual(
        signal,
        underlying_quote,
        config.get("costs", {}),
    )
    if (
        signal.get("action") != "propose_trade"
        or direction == "neutral"
        or masses[0] < float(profile.get("minimum_direction_mass", 0.55))
        or masses[0] - masses[1]
        < float(profile.get("minimum_direction_margin", 0.15))
    ):
        return {
            "allocation_id": allocation_id,
            "status": "no_trade",
            "reason": "signed return direction is neutral or insufficiently dominant",
            "signal_summary": summary,
            "considered": [],
            "selected_instrument": None,
            "counterfactual_2000": None,
            "short_equity_counterfactual": short_benchmark,
            "probability_ev_available": False,
            "probability_ev_usd": None,
            "raw_probability_used_for_ev": False,
        }

    move_pct = float(summary["conservative_move_pct"])
    considered: list[dict[str, Any]] = []
    if direction == "bullish":
        considered.append(
            _equity_candidate(
                underlying_quote,
                move_pct,
                account_state,
                config,
                profile,
            )
        )
    desired_option_type = "call" if direction == "bullish" else "put"
    for contract, option_quote in option_candidates:
        if contract.option_type != desired_option_type:
            continue
        considered.append(
            _option_candidate(
                contract,
                option_quote,
                signal=signal,
                spot=underlying_quote.last,
                move_pct=move_pct,
                account_state=account_state,
                config=config,
                profile=profile,
                now=now,
            )
        )
    eligible = [item for item in considered if item["eligible"]]
    selected = (
        max(eligible, key=lambda item: float(item["conservative_net_return_pct"]))
        if eligible
        else None
    )
    result = {
        "allocation_id": allocation_id,
        "status": "selected" if selected else "no_trade",
        "reason": None if selected else "no instrument cleared conservative executable hurdles",
        "signal_summary": summary,
        "considered": considered,
        "selected_instrument": selected,
        "counterfactual_2000": None,
        "short_equity_counterfactual": short_benchmark,
        "probability_ev_available": False,
        "probability_ev_usd": None,
        "raw_probability_used_for_ev": False,
    }
    if selected is not None:
        result["counterfactual_2000"] = build_same_instrument_counterfactual(
            result,
            nav_usd=float(profile.get("counterfactual_nav_usd", 2_000)),
        )
    return result
