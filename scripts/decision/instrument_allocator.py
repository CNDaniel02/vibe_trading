from __future__ import annotations

import math
from typing import Any
from uuid import uuid4

from scripts.core.models import Quote, parse_ts
from scripts.decision.signed_return_signal import (
    derive_signal_summary,
    validate_actionable_signal,
)
from scripts.options.models import OptionContract, OptionQuote
from scripts.options.scenario_pricing import reprice_option_scenarios


_SELECTION_POLICY_VERSION = "deterministic_risk_adjusted_v1"
_SELECTION_SCORE_METHOD = "conservative_scenario_pnl_over_deterministic_risk_v1"


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


def _risk_adjusted_metrics(
    *,
    scenario_pnl_usd: float,
    deterministic_risk_usd: float,
    nav_usd: float,
) -> dict[str, Any]:
    return_on_nav = scenario_pnl_usd / nav_usd if nav_usd > 0 else 0.0
    risk_pct = deterministic_risk_usd / nav_usd if nav_usd > 0 else 0.0
    score = return_on_nav / risk_pct if risk_pct > 0 else None
    return {
        "scenario_pnl_usd": round(scenario_pnl_usd, 6),
        "scenario_return_on_account_nav": round(return_on_nav, 10),
        "deterministic_risk_usd": round(deterministic_risk_usd, 6),
        "deterministic_risk_pct_of_nav": round(risk_pct, 10),
        "selection_score": round(score, 10) if score is not None else None,
        "selection_score_method": _SELECTION_SCORE_METHOD,
    }


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
    deterministic_risk = quantity * unit_stop_risk
    metrics = _risk_adjusted_metrics(
        scenario_pnl_usd=(exit_price - entry) * quantity,
        deterministic_risk_usd=deterministic_risk,
        nav_usd=nav,
    )
    return {
        "instrument_type": "equity",
        "ticker": quote.symbol,
        "entry_price": round(entry, 6),
        "scenario_exit_price": round(exit_price, 6),
        "conservative_move_pct": move_pct,
        "forecast_remaining_move_pct": move_pct,
        "conservative_net_return_pct": round(net_return, 8),
        "break_even_move_pct": round((entry / quote.last - 1) * 100, 6),
        "planned_stop_price": round(stop_price, 6),
        "quantity": round(quantity, 6),
        "notional_usd": round(quantity * entry, 6),
        "risk_usd": round(deterministic_risk, 6),
        **metrics,
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
    spot: float,
    move_pct: float,
    account_state: dict[str, Any],
    config: dict[str, Any],
    profile: dict[str, Any],
    now: str,
    elapsed_calendar_days: float,
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
        elapsed_calendar_days=elapsed_calendar_days,
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
        elapsed_calendar_days=elapsed_calendar_days,
        conservative_iv_shift=min(iv_shifts),
        costs=config.get("options_costs", {}),
    )
    deterministic_risk = unit_risk * quantity
    metrics = _risk_adjusted_metrics(
        scenario_pnl_usd=float(repricing["conservative_net_pnl_usd"]) * quantity,
        deterministic_risk_usd=deterministic_risk,
        nav_usd=nav,
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
        "risk_usd": round(deterministic_risk, 6),
        **metrics,
        "spread_pct": round(quote.spread_pct(), 8),
        "preferred_spread": preferred,
        "conservative_move_pct": move_pct,
        "forecast_remaining_move_pct": move_pct,
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
    elapsed_calendar_days: float,
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
            elapsed_calendar_days=elapsed_calendar_days,
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
    *,
    remaining_move_pct: float,
) -> dict[str, Any] | None:
    summary = derive_signal_summary(signal)
    if summary["direction"] != "bearish":
        return None
    entry = max(0.0, quote.bid - _slippage(quote.bid, costs, option=False))
    scenario_mid = quote.last * (1 + remaining_move_pct / 100)
    cover = scenario_mid + (quote.ask - quote.bid) / 2
    cover += _slippage(cover, costs, option=False)
    return {
        "benchmark_name": "short_equity_counterfactual",
        "ticker": quote.symbol,
        "entry_price": round(entry, 6),
        "scenario_cover_price": round(cover, 6),
        "forecast_remaining_move_pct": remaining_move_pct,
        "scenario_net_return_pct": round((entry - cover) / entry, 8) if entry else None,
        "probability_status": "uncalibrated",
        "creates_order": False,
        "enters_account": False,
        "merged_with_long_put_pnl": False,
    }


def _no_trade_allocation(
    allocation_id: str,
    reason: str,
    signal_summary: dict[str, Any],
    *,
    planned_exit_at: str | None = None,
    elapsed_calendar_days: float | None = None,
) -> dict[str, Any]:
    result = {
        "allocation_id": allocation_id,
        "status": "no_trade",
        "reason": reason,
        "signal_summary": signal_summary,
        "considered": [],
        "selected_instrument": None,
        "counterfactual_2000": None,
        "short_equity_counterfactual": None,
        "selection_policy_version": _SELECTION_POLICY_VERSION,
        "probability_ev_available": False,
        "probability_ev_usd": None,
        "raw_probability_used_for_ev": False,
    }
    if planned_exit_at is not None:
        result["planned_exit_at"] = planned_exit_at
    if elapsed_calendar_days is not None:
        result["scenario_elapsed_calendar_days"] = elapsed_calendar_days
    return result


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
        risk_usd = max_quantity * unit_risk
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
    *,
    planned_exit_at: str,
) -> dict[str, Any]:
    profile = config.get("strategies", {}).get("ai_instrument_allocator_v1", {})
    allocation_id = f"aia_{uuid4().hex}"
    try:
        validate_actionable_signal(
            signal,
            now,
            planned_exit_at=planned_exit_at,
        )
        summary = derive_signal_summary(signal)
    except (KeyError, TypeError, ValueError) as exc:
        return _no_trade_allocation(
            allocation_id,
            f"invalid actionable signal: {exc}",
            {},
        )
    masses = sorted(
        (
            float(summary["bullish_probability"]),
            float(summary["bearish_probability"]),
            float(summary["neutral_probability"]),
        ),
        reverse=True,
    )
    direction = str(summary["direction"])
    if (
        signal.get("action") != "propose_trade"
        or direction == "neutral"
        or masses[0] < float(profile.get("minimum_direction_mass", 0.55))
        or masses[0] - masses[1]
        < float(profile.get("minimum_direction_margin", 0.15))
    ):
        return _no_trade_allocation(
            allocation_id,
            "signed return direction is neutral or insufficiently dominant",
            summary,
        )

    try:
        elapsed_calendar_days = (
            parse_ts(planned_exit_at) - parse_ts(now)
        ).total_seconds() / 86_400
    except (TypeError, ValueError) as exc:
        return _no_trade_allocation(
            allocation_id,
            f"invalid planned exit: {exc}",
            summary,
        )
    if elapsed_calendar_days <= 0:
        return _no_trade_allocation(
            allocation_id,
            "invalid planned exit: planned_exit_at must be after decision time",
            summary,
        )

    reference_price = signal.get("forecast_reference_price")
    reference_time = signal.get("forecast_reference_time")
    try:
        reference_price = float(reference_price)
        reference_is_valid = (
            reference_price > 0
            and isinstance(reference_time, str)
            and parse_ts(reference_time) <= parse_ts(now)
        )
    except (TypeError, ValueError):
        reference_is_valid = False
    if not reference_is_valid:
        return _no_trade_allocation(
            allocation_id,
            "missing or invalid forecast reference",
            summary,
            planned_exit_at=planned_exit_at,
            elapsed_calendar_days=elapsed_calendar_days,
        )

    forecast_move_pct = float(summary["conservative_move_pct"])
    forecast_target_price = reference_price * (1 + forecast_move_pct / 100)
    realized_move_pct = (underlying_quote.last / reference_price - 1) * 100
    remaining_move_pct = (
        forecast_target_price / underlying_quote.last - 1
    ) * 100

    desired_option_type = "call" if direction == "bullish" else "put"
    implied_candidates = [
        (contract, option_quote)
        for contract, option_quote in option_candidates
        if contract.option_type == desired_option_type
        and option_quote.implied_volatility is not None
        and option_quote.implied_volatility > 0
    ]
    nearest_implied = (
        min(
            implied_candidates,
            key=lambda item: (
                abs(item[0].strike_price - underlying_quote.last),
                item[0].expiration_date,
                item[0].option_id,
            ),
        )
        if implied_candidates
        else None
    )
    if nearest_implied is None:
        implied_move_pct = None
        implied_ratio = None
        implied_method = None
        implied_option_id = None
        implied_volatility = None
    else:
        implied_option_id = nearest_implied[0].option_id
        implied_volatility = float(nearest_implied[1].implied_volatility)
        implied_move_pct = implied_volatility * math.sqrt(
            elapsed_calendar_days / 365
        ) * 100
        implied_ratio = abs(remaining_move_pct) / implied_move_pct
        implied_method = "nearest_candidate_iv_sqrt_calendar_time"

    forecast_context = {
        "forecast_reference_price": reference_price,
        "forecast_reference_time": reference_time,
        "forecast_conservative_move_pct": forecast_move_pct,
        "forecast_target_price": forecast_target_price,
        "realized_move_since_reference_pct": realized_move_pct,
        "remaining_move_pct": remaining_move_pct,
        "market_implied_move_pct": implied_move_pct,
        "forecast_to_implied_move_ratio": implied_ratio,
        "market_implied_move_method": implied_method,
        "market_implied_move_option_id": implied_option_id,
        "market_implied_volatility": implied_volatility,
        "forecast_exceeds_market_implied_move": (
            abs(remaining_move_pct) >= implied_move_pct
            if implied_move_pct is not None
            else None
        ),
        "planned_exit_at": planned_exit_at,
        "scenario_elapsed_calendar_days": elapsed_calendar_days,
    }
    short_benchmark = build_short_equity_counterfactual(
        signal,
        underlying_quote,
        config.get("costs", {}),
        remaining_move_pct=remaining_move_pct,
    )
    considered: list[dict[str, Any]] = []
    if direction == "bullish":
        considered.append(
            _equity_candidate(
                underlying_quote,
                remaining_move_pct,
                account_state,
                config,
                profile,
            )
        )
    for contract, option_quote in option_candidates:
        if contract.option_type != desired_option_type:
            continue
        considered.append(
            _option_candidate(
                contract,
                option_quote,
                spot=underlying_quote.last,
                move_pct=remaining_move_pct,
                account_state=account_state,
                config=config,
                profile=profile,
                now=now,
                elapsed_calendar_days=elapsed_calendar_days,
            )
        )
    eligible = [item for item in considered if item["eligible"]]
    selected = (
        max(
            eligible,
            key=lambda item: (
                float(item["selection_score"]),
                float(item["scenario_return_on_account_nav"]),
                -float(item["deterministic_risk_pct_of_nav"]),
                item["instrument_type"] == "equity",
                str(item.get("option_id", "")),
            ),
        )
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
        "selection_policy_version": _SELECTION_POLICY_VERSION,
        "probability_ev_available": False,
        "probability_ev_usd": None,
        "raw_probability_used_for_ev": False,
        **forecast_context,
    }
    if selected is not None:
        result["counterfactual_2000"] = build_same_instrument_counterfactual(
            result,
            nav_usd=float(profile.get("counterfactual_nav_usd", 2_000)),
        )
    return result
