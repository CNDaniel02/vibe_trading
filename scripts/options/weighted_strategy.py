from __future__ import annotations

from typing import Any

from scripts.strategies.technical_scoring import DEFAULT_WEIGHTS, directional_feature_scores, weighted_score


STRATEGY_NAME = "long_directional_options_v2_weighted"


def _event_scores(snapshot: dict[str, Any]) -> tuple[float, float, list[str]]:
    positive = 0.0
    negative = 0.0
    evidence: list[str] = []
    signal = snapshot.get("market_data", {}).get("catalyst_signal")
    events = [signal] if isinstance(signal, dict) else []
    events.extend(item for item in snapshot.get("available_news", []) if isinstance(item, dict))
    for event in events:
        direction = str(event.get("direction", "neutral")).lower()
        confidence = float(event.get("confidence", event.get("materiality", 0.5)) or 0.0)
        relevance = float(event.get("ticker_relevance", 1.0) or 0.0)
        tier = int(event.get("source_tier", 4) or 4)
        quality = {1: 1.0, 2: 0.85, 3: 0.60, 4: 0.35}.get(tier, 0.35)
        score = max(0.0, min(1.0, confidence * relevance * quality))
        if direction in {"positive", "bullish"}:
            positive = max(positive, score)
        elif direction in {"negative", "bearish"}:
            negative = max(negative, score)
        headline = event.get("headline") or event.get("catalyst_summary")
        if headline and score > 0:
            evidence.append(str(headline)[:200])
    return positive, negative, evidence[:3]


def decide_weighted_option_direction(
    snapshot: dict[str, Any],
    runtime_config: dict[str, Any],
) -> dict[str, Any]:
    profile = runtime_config.get("strategies", {}).get(STRATEGY_NAME, {})
    signals = snapshot.get("technical_signals", {})
    market = snapshot.get("market_data", {})
    result = {
        "strategy": STRATEGY_NAME,
        "baseline_strategy": "long_directional_options_v1",
        "snapshot_id": snapshot.get("snapshot_id"),
        "ticker": snapshot.get("ticker"),
        "action": "no_trade",
        "option_type": None,
        "score": 0.0,
        "call_score": 0.0,
        "put_score": 0.0,
        "event_scores": {},
        "reasons": [],
    }
    if snapshot.get("market_session") != "regular":
        result["reasons"].append("outside regular market session")
        return result
    if not bool(market.get("history_fresh", False)):
        result["reasons"].append("completed OHLCV history is stale")
        return result
    event_days = int(market.get("binary_event_within_days", 99))
    if event_days <= int(profile.get("reject_binary_event_within_days", 7)):
        result["reasons"].append("binary earnings event inside exclusion window")
        return result
    chase = float(signals.get("chase_score") or 0)
    hard_max_chase = float(profile.get("hard_max_chase_score", 0.95))
    if chase > hard_max_chase:
        result["reasons"].append(f"extreme chase risk above {hard_max_chase:g}")
        return result

    technical_weights = {
        key: float(profile.get("technical_weights", {}).get(key, value))
        for key, value in DEFAULT_WEIGHTS.items()
    }
    call_technical = weighted_score(directional_feature_scores(snapshot, "bullish"), technical_weights)
    put_technical = weighted_score(directional_feature_scores(snapshot, "bearish"), technical_weights)
    positive_event, negative_event, evidence = _event_scores(snapshot)
    event_weight = float(profile.get("event_weight", 0.30))
    technical_weight = 1.0 - event_weight
    if max(positive_event, negative_event) > 0:
        call_score = technical_weight * call_technical + event_weight * positive_event
        put_score = technical_weight * put_technical + event_weight * negative_event
    else:
        # Missing event evidence is neutral, not a zero-valued bearish/bullish
        # feature. Diluting technical scores here prevented the options paper
        # line from ever reaching contract selection on ordinary trading days.
        call_score = call_technical
        put_score = put_technical
    result["call_score"] = round(call_score, 6)
    result["put_score"] = round(put_score, 6)
    result["event_scores"] = {"positive": positive_event, "negative": negative_event}
    result["event_evidence"] = evidence

    minimum = float(profile.get("minimum_entry_score", 0.56))
    minimum_negative_event = float(profile.get("minimum_company_negative_event_score", 0.50))
    minimum_downside_technical = float(profile.get("minimum_downside_technical_score", 0.58))
    if put_score >= max(call_score, minimum) and (
        negative_event >= minimum_negative_event or put_technical >= minimum_downside_technical
    ):
        result.update(action="buy_to_open", option_type="put", score=round(put_score, 6))
        if negative_event >= minimum_negative_event:
            result["reasons"].append("company-specific negative evidence supports long put")
        else:
            result["reasons"].append("weighted downside technical score supports long put")
        return result
    if call_score >= max(put_score, minimum):
        result.update(action="buy_to_open", option_type="call", score=round(call_score, 6))
        result["reasons"].append("weighted bullish evidence supports long call")
        return result
    result["score"] = round(max(call_score, put_score), 6)
    result["reasons"].append(f"best weighted option score below {minimum:.3f}")
    return result
