from __future__ import annotations

from typing import Any

from scripts.core.models import Quote
from scripts.risk.risk_gate import validate_quote


def quote_from_snapshot(snapshot: dict[str, Any]) -> Quote | None:
    raw = snapshot.get("market_data", {}).get("quote")
    if not raw:
        return None
    return Quote(
        symbol=snapshot["ticker"],
        bid=float(raw.get("bid", 0)),
        ask=float(raw.get("ask", 0)),
        last=float(raw.get("last", 0)),
        asof=str(raw.get("asof", snapshot["data_cutoff_time"])),
        source=str(raw.get("source", "snapshot")),
        avg_daily_volume_usd=float(raw.get("avg_daily_volume_usd", 0)) if raw.get("avg_daily_volume_usd") is not None else None,
        asset_class=str(raw.get("asset_class", "us_equity")),
        is_otc=bool(raw.get("is_otc", False)),
        is_leveraged_etf=bool(raw.get("is_leveraged_etf", False)),
        is_inverse_etf=bool(raw.get("is_inverse_etf", False)),
        halted=bool(raw.get("halted", False)),
    )


def run_regime_agent(snapshot: dict[str, Any]) -> dict[str, Any]:
    market = snapshot["market_data"]
    explicit = str(market.get("market_regime", "neutral"))
    benchmark_move = float(market.get("benchmark_change_1d_pct", 0))
    volatility_move = float(market.get("volatility_change_pct", 0))
    risk_off = explicit == "risk_off" or benchmark_move <= -1.5 or volatility_move >= 10
    status = "risk_off" if risk_off else ("risk_on" if explicit == "risk_on" or benchmark_move >= 0.5 else "neutral")
    return {
        "status": status,
        "eligible": snapshot["market_session"] == "regular" and not risk_off,
        "benchmark_change_1d_pct": benchmark_move,
        "volatility_change_pct": volatility_move,
        "reasons": ["Risk-off market regime blocks new long entries."] if risk_off else [],
    }


def run_technical_agent(snapshot: dict[str, Any], runtime_config: dict[str, Any]) -> dict[str, Any]:
    quote = quote_from_snapshot(snapshot)
    quote_decision = validate_quote(
        quote,
        snapshot["decision_time"],
        int(runtime_config["paper"].get("quote_stale_after_seconds", 60)),
        runtime_config["universe"],
    )
    signals = snapshot["technical_signals"]
    relative_strength = float(signals.get("relative_strength_20d", 0))
    price_change_1d = float(signals.get("price_change_1d_pct", 0))
    price_change_5d = float(signals.get("price_change_5d_pct", 0))
    raw_volume_ratio = signals.get("volume_ratio")
    volume_ratio = float(raw_volume_ratio) if raw_volume_ratio is not None else None
    chase_score = float(signals.get("chase_score", 0))
    profile = runtime_config.get("strategies", {}).get("relative_strength_v1", {})
    min_relative_strength = float(profile.get("min_relative_strength_20d_pct", 0.5))
    min_price_change_1d = float(profile.get("min_price_change_1d_pct", -999))
    min_price_change_5d = float(profile.get("min_price_change_5d_pct", 0))
    min_volume_ratio = float(profile.get("min_volume_ratio", 0.8))
    require_volume = bool(profile.get("require_intraday_volume_confirmation", False))
    volume_confirmed = volume_ratio is not None and volume_ratio >= min_volume_ratio
    reasons: list[str] = []
    if not quote_decision.approved:
        reasons.append(quote_decision.reason)
    if relative_strength < min_relative_strength:
        reasons.append(f"20-day relative strength below {min_relative_strength}")
    if price_change_1d < min_price_change_1d:
        reasons.append(f"1-day price change below {min_price_change_1d}")
    if price_change_5d < min_price_change_5d:
        reasons.append(f"5-day price change below {min_price_change_5d}")
    if require_volume and volume_ratio is None:
        reasons.append("intraday volume confirmation unavailable")
    elif require_volume and not volume_confirmed:
        reasons.append(f"volume ratio below {min_volume_ratio}")
    candidate = bool(
        quote_decision.approved
        and snapshot["market_session"] == "regular"
        and relative_strength >= min_relative_strength
        and price_change_1d >= min_price_change_1d
        and price_change_5d >= min_price_change_5d
        and (volume_confirmed or not require_volume)
    )
    return {
        "candidate": candidate,
        "quote_valid": quote_decision.approved,
        "quote_reason": quote_decision.reason,
        "relative_strength_20d": relative_strength,
        "price_change_1d_pct": price_change_1d,
        "price_change_5d_pct": price_change_5d,
        "volume_ratio": volume_ratio,
        "volume_data_available": volume_ratio is not None,
        "chase_score": chase_score,
        "reasons": reasons,
        "thresholds": {
            "min_relative_strength_20d_pct": min_relative_strength,
            "min_price_change_1d_pct": min_price_change_1d,
            "min_price_change_5d_pct": min_price_change_5d,
            "min_volume_ratio": min_volume_ratio,
        },
    }
