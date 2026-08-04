from __future__ import annotations

from dataclasses import asdict, dataclass
from math import erf, exp, log, pi, sqrt


@dataclass(frozen=True)
class EuropeanOptionEstimate:
    price: float
    delta: float
    gamma: float
    theta_per_day: float
    vega_per_vol_point: float

    def to_dict(self) -> dict[str, float]:
        return asdict(self)


def _cdf(value: float) -> float:
    return 0.5 * (1 + erf(value / sqrt(2)))


def _pdf(value: float) -> float:
    return exp(-0.5 * value * value) / sqrt(2 * pi)


def black_scholes_estimate(
    *,
    option_type: str,
    spot: float,
    strike: float,
    years_to_expiry: float,
    volatility: float,
    risk_free_rate: float = 0.0,
    dividend_yield: float = 0.0,
) -> EuropeanOptionEstimate:
    """Independent European reference only; never used as a simulated fill."""
    if option_type not in {"call", "put"}:
        raise ValueError("option_type must be call or put")
    if spot <= 0 or strike <= 0 or years_to_expiry <= 0 or volatility <= 0:
        raise ValueError("spot, strike, time, and volatility must be positive")
    root_time = sqrt(years_to_expiry)
    d1 = (log(spot / strike) + (risk_free_rate - dividend_yield + 0.5 * volatility**2) * years_to_expiry) / (volatility * root_time)
    d2 = d1 - volatility * root_time
    discounted_spot = spot * exp(-dividend_yield * years_to_expiry)
    discounted_strike = strike * exp(-risk_free_rate * years_to_expiry)
    density = _pdf(d1)
    gamma = exp(-dividend_yield * years_to_expiry) * density / (spot * volatility * root_time)
    vega = discounted_spot * density * root_time / 100
    common_theta = -(discounted_spot * density * volatility) / (2 * root_time)
    if option_type == "call":
        price = discounted_spot * _cdf(d1) - discounted_strike * _cdf(d2)
        delta = exp(-dividend_yield * years_to_expiry) * _cdf(d1)
        theta = common_theta - risk_free_rate * discounted_strike * _cdf(d2) + dividend_yield * discounted_spot * _cdf(d1)
    else:
        price = discounted_strike * _cdf(-d2) - discounted_spot * _cdf(-d1)
        delta = exp(-dividend_yield * years_to_expiry) * (_cdf(d1) - 1)
        theta = common_theta + risk_free_rate * discounted_strike * _cdf(-d2) - dividend_yield * discounted_spot * _cdf(-d1)
    return EuropeanOptionEstimate(
        price=round(price, 6),
        delta=round(delta, 6),
        gamma=round(gamma, 6),
        theta_per_day=round(theta / 365, 6),
        vega_per_vol_point=round(vega, 6),
    )
