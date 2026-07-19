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
    volume_ratio = float(signals.get("volume_ratio", 0))
    chase_score = float(signals.get("chase_score", 0))
    candidate = bool(
        quote_decision.approved
        and snapshot["market_session"] == "regular"
        and relative_strength >= 0.5
        and price_change_5d > 0
        and volume_ratio >= 0.8
    )
    return {
        "candidate": candidate,
        "quote_valid": quote_decision.approved,
        "quote_reason": quote_decision.reason,
        "relative_strength_20d": relative_strength,
        "price_change_1d_pct": price_change_1d,
        "price_change_5d_pct": price_change_5d,
        "volume_ratio": volume_ratio,
        "chase_score": chase_score,
    }
