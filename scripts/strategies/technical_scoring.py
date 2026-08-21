from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from scripts.core.state import JsonStateStore


FEATURES = (
    "relative_strength",
    "momentum_1d",
    "momentum_5d",
    "volume_confirmation",
    "market_regime",
    "chase_quality",
)

DEFAULT_WEIGHTS = {
    "relative_strength": 0.30,
    "momentum_1d": 0.15,
    "momentum_5d": 0.20,
    "volume_confirmation": 0.15,
    "market_regime": 0.10,
    "chase_quality": 0.10,
}


def clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, value))


def directional_feature_scores(snapshot: dict[str, Any], direction: str = "bullish") -> dict[str, float]:
    signals = snapshot.get("technical_signals", {})
    market = snapshot.get("market_data", {})
    sign = 1.0 if direction == "bullish" else -1.0
    rs20 = float(signals.get("relative_strength_20d") or 0)
    move_1d = float(signals.get("price_change_1d_pct") or 0)
    move_5d = float(signals.get("price_change_5d_pct") or 0)
    raw_volume = signals.get("volume_ratio")
    volume = float(raw_volume) if raw_volume is not None else None
    chase = float(signals.get("chase_score") or 0)
    regime = str(market.get("market_regime", "neutral"))
    if direction == "bullish":
        regime_score = {"risk_on": 0.70, "neutral": 0.55, "risk_off": 0.25}.get(regime, 0.50)
    else:
        regime_score = {"risk_off": 0.70, "neutral": 0.55, "risk_on": 0.35}.get(regime, 0.50)
    return {
        "relative_strength": clamp(0.50 + sign * rs20 / 8.0),
        "momentum_1d": clamp(0.50 + sign * move_1d / 6.0),
        "momentum_5d": clamp(0.50 + sign * move_5d / 12.0),
        "volume_confirmation": clamp(volume / 1.5) if volume is not None else 0.42,
        "market_regime": regime_score,
        "chase_quality": clamp(1.0 - chase),
    }


def weighted_score(feature_scores: dict[str, float], weights: dict[str, float]) -> float:
    return round(
        sum(float(weights.get(name, 0)) * float(feature_scores.get(name, 0)) for name in FEATURES),
        6,
    )


class AdaptiveWeightStore:
    """Exponentially reweights fixed experts using observed squared loss."""

    FILENAME = "strategy_weights.json"

    def __init__(self, root: str | Path, profile: dict[str, Any]) -> None:
        self.store = JsonStateStore(root)
        self.filename = str(profile.get("weight_state_file", self.FILENAME))
        configured = profile.get("fixed_weights", DEFAULT_WEIGHTS)
        self.prior = self._normalize({name: float(configured.get(name, DEFAULT_WEIGHTS[name])) for name in FEATURES})
        adaptive = profile.get("adaptive_weights", {})
        self.enabled = bool(adaptive.get("enabled", True))
        self.eta = float(adaptive.get("learning_rate", 1.5))
        self.minimum_samples = int(adaptive.get("minimum_labeled_samples", 30))
        self.weight_floor = float(adaptive.get("weight_floor", 0.05))

    def current(self) -> dict[str, Any]:
        state = self._read()
        samples = int(state["labeled_samples"])
        if not self.enabled or samples < self.minimum_samples:
            weights = self.prior
            mode = "fixed_warmup" if self.enabled else "fixed"
        else:
            average_losses = {
                name: float(state["cumulative_squared_loss"].get(name, 0.0)) / max(1, samples)
                for name in FEATURES
            }
            raw = {
                name: max(
                    self.weight_floor,
                    self.prior[name] * math.exp(-self.eta * average_losses[name]),
                )
                for name in FEATURES
            }
            weights = self._normalize(raw)
            mode = "adaptive_min_loss"
        return {
            "mode": mode,
            "labeled_samples": samples,
            "weights": {name: round(weights[name], 6) for name in FEATURES},
            "cumulative_squared_loss": state["cumulative_squared_loss"],
        }

    def observe(self, feature_scores: dict[str, float], outcome: float) -> dict[str, Any]:
        state = self._read()
        target = clamp(float(outcome))
        for name in FEATURES:
            prediction = clamp(float(feature_scores.get(name, 0.5)))
            state["cumulative_squared_loss"][name] = round(
                float(state["cumulative_squared_loss"].get(name, 0.0)) + (prediction - target) ** 2,
                10,
            )
        state["labeled_samples"] = int(state["labeled_samples"]) + 1
        self.store.write_json(self.filename, state)
        return self.current()

    def _read(self) -> dict[str, Any]:
        default = {
            "version": 1,
            "labeled_samples": 0,
            "cumulative_squared_loss": {name: 0.0 for name in FEATURES},
        }
        state = self.store.read_json(self.filename, default)
        state.setdefault("labeled_samples", 0)
        losses = state.setdefault("cumulative_squared_loss", {})
        for name in FEATURES:
            losses.setdefault(name, 0.0)
        return state

    @staticmethod
    def _normalize(values: dict[str, float]) -> dict[str, float]:
        total = sum(max(0.0, value) for value in values.values())
        if total <= 0:
            return dict(DEFAULT_WEIGHTS)
        return {name: max(0.0, values[name]) / total for name in FEATURES}
