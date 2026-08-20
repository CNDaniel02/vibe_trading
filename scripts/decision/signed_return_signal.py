from __future__ import annotations

import math
from typing import Any

from scripts.core.models import parse_ts


SIGNED_RETURN_BUCKETS = (
    "return_lt_minus_5_pct",
    "return_minus_5_to_minus_2_pct",
    "return_minus_2_to_minus_0_5_pct",
    "return_minus_0_5_to_plus_0_5_pct",
    "return_plus_0_5_to_plus_2_pct",
    "return_plus_2_to_plus_5_pct",
    "return_gt_plus_5_pct",
)

_BEARISH_BUCKETS = SIGNED_RETURN_BUCKETS[:3]
_NEUTRAL_BUCKET = SIGNED_RETURN_BUCKETS[3]
_BULLISH_BUCKETS = SIGNED_RETURN_BUCKETS[4:]
_DIRECTIONAL_SCENARIOS = {
    "bearish": (
        ("return_minus_2_to_minus_0_5_pct", -0.5),
        ("return_minus_5_to_minus_2_pct", -2.0),
        ("return_lt_minus_5_pct", -5.0),
    ),
    "bullish": (
        ("return_plus_0_5_to_plus_2_pct", 0.5),
        ("return_plus_2_to_plus_5_pct", 2.0),
        ("return_gt_plus_5_pct", 5.0),
    ),
}
_CONSERVATIVE_TAIL_FRACTION = 0.5


def validate_signed_return_signal(signal: dict[str, Any]) -> None:
    if signal.get("probability_status") != "uncalibrated":
        raise ValueError("raw model probabilities must remain uncalibrated")
    if signal.get("horizon") not in {
        "intraday_close",
        "next_close",
        "two_to_five_days",
    }:
        raise ValueError("unsupported signal horizon")
    buckets = signal.get("signed_return_probability_buckets")
    if not isinstance(buckets, dict) or set(buckets) != set(SIGNED_RETURN_BUCKETS):
        raise ValueError("signal must contain exactly the configured signed buckets")
    try:
        probabilities = [float(buckets[name]) for name in SIGNED_RETURN_BUCKETS]
    except (TypeError, ValueError) as exc:
        raise ValueError("signed bucket probabilities must be numeric") from exc
    if any(not math.isfinite(value) for value in probabilities):
        raise ValueError("signed bucket probabilities must be finite")
    if any(value < 0 or value > 1 for value in probabilities):
        raise ValueError("signed bucket probabilities must be inside [0, 1]")
    if abs(sum(probabilities) - 1.0) > 1e-6:
        raise ValueError("signed bucket probabilities must sum to 1")


def validate_actionable_signal(signal: dict[str, Any], decision_time: str) -> None:
    validate_signed_return_signal(signal)
    action = signal.get("action")
    if action not in {"propose_trade", "no_trade"}:
        raise ValueError("unsupported signal action")
    if action != "propose_trade":
        return

    for field in ("thesis", "entry_condition", "invalidation_condition"):
        if not isinstance(signal.get(field), str) or not signal[field].strip():
            raise ValueError(f"actionable signal requires non-empty {field}")

    valid_until = signal.get("thesis_valid_until")
    if not isinstance(valid_until, str) or not valid_until.strip():
        raise ValueError("actionable signal requires thesis_valid_until")
    try:
        valid_until_time = parse_ts(valid_until)
        current = parse_ts(decision_time)
    except (TypeError, ValueError) as exc:
        raise ValueError("actionable signal timestamps must be valid") from exc
    if valid_until_time <= current:
        raise ValueError("actionable signal thesis_valid_until must be in the future")

    holding_days = signal.get("max_holding_trading_days")
    if isinstance(holding_days, bool) or not isinstance(holding_days, int):
        raise ValueError("actionable signal max_holding_trading_days must be an integer")
    expected = {
        "intraday_close": {0},
        "next_close": {1},
        "two_to_five_days": {2, 3, 4, 5},
    }[str(signal["horizon"])]
    if holding_days not in expected:
        raise ValueError("actionable signal holding period does not match horizon")


def derive_signal_summary(signal: dict[str, Any]) -> dict[str, Any]:
    validate_signed_return_signal(signal)
    buckets = signal["signed_return_probability_buckets"]
    bearish = sum(float(buckets[name]) for name in _BEARISH_BUCKETS)
    neutral = float(buckets[_NEUTRAL_BUCKET])
    bullish = sum(float(buckets[name]) for name in _BULLISH_BUCKETS)
    masses = {"bearish": bearish, "neutral": neutral, "bullish": bullish}
    direction = max(masses, key=lambda name: (masses[name], name == "neutral"))
    direction_buckets = {
        "bearish": _BEARISH_BUCKETS,
        "neutral": (_NEUTRAL_BUCKET,),
        "bullish": _BULLISH_BUCKETS,
    }[direction]
    dominant = max(
        direction_buckets,
        key=lambda name: (float(buckets[name]), -SIGNED_RETURN_BUCKETS.index(name)),
    )
    scenarios: list[dict[str, Any]] = []
    conservative_move = 0.0
    if direction in _DIRECTIONAL_SCENARIOS:
        tail_mass = masses[direction] * _CONSERVATIVE_TAIL_FRACTION
        remaining = tail_mass
        weighted_move = 0.0
        for bucket, move_pct in _DIRECTIONAL_SCENARIOS[direction]:
            included = min(float(buckets[bucket]), remaining)
            if included <= 0:
                continue
            scenarios.append(
                {
                    "bucket": bucket,
                    "bucket_probability": round(float(buckets[bucket]), 10),
                    "included_probability": round(included, 10),
                    "move_pct": move_pct,
                }
            )
            weighted_move += included * move_pct
            remaining -= included
            if remaining <= 1e-12:
                break
        if tail_mass > 0:
            conservative_move = weighted_move / tail_mass
    return {
        "bearish_probability": round(bearish, 10),
        "neutral_probability": round(neutral, 10),
        "bullish_probability": round(bullish, 10),
        "direction": direction,
        "dominant_signed_bucket": dominant,
        "conservative_move_pct": round(conservative_move, 10),
        "conservative_move_method": "directional_lower_tail_mean",
        "conservative_tail_fraction": _CONSERVATIVE_TAIL_FRACTION,
        "conservative_scenarios": scenarios,
        "probability_status": "uncalibrated",
    }
