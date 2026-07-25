from __future__ import annotations

from typing import Any


STRATEGY_NAME = "long_directional_options_v1"


def decide_option_direction(
    snapshot: dict[str, Any],
    runtime_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    signals = snapshot.get("technical_signals", {})
    market = snapshot.get("market_data", {})
    profile = (runtime_config or {}).get("strategies", {}).get(STRATEGY_NAME, {})
    min_volume_ratio = float(profile.get("min_volume_ratio", 1.0))
    max_chase_score = float(profile.get("max_chase_score", 0.75))
    bullish_min_rs20 = float(profile.get("bullish_min_relative_strength_20d_pct", 1.0))
    bullish_min_move_5d = float(profile.get("bullish_min_price_change_5d_pct", 1.0))
    bullish_min_move_1d = float(profile.get("bullish_min_price_change_1d_pct", -1.0))
    bearish_max_rs20 = float(profile.get("bearish_max_relative_strength_20d_pct", -0.5))
    bearish_max_move_5d = float(profile.get("bearish_max_price_change_5d_pct", -1.5))
    bearish_max_move_1d = float(profile.get("bearish_max_price_change_1d_pct", 0.0))
    rs20 = float(signals.get("relative_strength_20d", 0))
    move_1d = float(signals.get("price_change_1d_pct", 0))
    move_5d = float(signals.get("price_change_5d_pct", 0))
    raw_volume = signals.get("volume_ratio")
    volume = float(raw_volume) if raw_volume is not None else None
    chase = float(signals.get("chase_score", 0))
    regime = str(market.get("market_regime", "neutral"))
    event_days = int(market.get("binary_event_within_days", 99))
    result = {
        "strategy": STRATEGY_NAME,
        "snapshot_id": snapshot.get("snapshot_id"),
        "ticker": snapshot.get("ticker"),
        "action": "no_trade",
        "option_type": None,
        "reasons": [],
    }
    if snapshot.get("market_session") != "regular":
        result["reasons"].append("outside regular market session")
    elif event_days <= 7:
        result["reasons"].append("binary earnings event inside exclusion window")
    elif volume is None:
        result["reasons"].append("intraday volume confirmation unavailable")
    elif volume < min_volume_ratio:
        result["reasons"].append(f"volume confirmation below {min_volume_ratio:g}")
    elif chase > max_chase_score:
        result["reasons"].append(f"chase risk above {max_chase_score:g}")
    elif (
        regime != "risk_off"
        and rs20 >= bullish_min_rs20
        and move_5d >= bullish_min_move_5d
        and move_1d > bullish_min_move_1d
    ):
        result.update(action="buy_to_open", option_type="call")
        result["reasons"].append("confirmed positive relative-strength continuation")
    elif (
        regime == "risk_off"
        and rs20 <= bearish_max_rs20
        and move_5d <= bearish_max_move_5d
        and move_1d < bearish_max_move_1d
    ):
        result.update(action="buy_to_open", option_type="put")
        result["reasons"].append("risk-off downside continuation with relative weakness")
    else:
        result["reasons"].append("directional option threshold not met")
    return result
