from __future__ import annotations

from typing import Any


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
    probabilities = [float(buckets[name]) for name in SIGNED_RETURN_BUCKETS]
    if any(value < 0 or value > 1 for value in probabilities):
        raise ValueError("signed bucket probabilities must be inside [0, 1]")
    if abs(sum(probabilities) - 1.0) > 1e-6:
        raise ValueError("signed bucket probabilities must sum to 1")


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
