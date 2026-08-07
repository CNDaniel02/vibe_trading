from __future__ import annotations

import json
import time
from typing import Any

from scripts.llm.base_provider import LLMProvider, ProviderRequest, ProviderResponse, ProviderUsage
from scripts.llm.schemas import validate_schema
from scripts.llm.usage_tracker import UsageTracker


class MockProvider(LLMProvider):
    """Deterministic provider for tests and reproducible eval baselines."""

    def __init__(self, tracker: UsageTracker | None = None) -> None:
        self.tracker = tracker or UsageTracker()
        self.model = "deterministic-mock-v1"

    def generate(self, request: ProviderRequest) -> ProviderResponse:
        started = time.perf_counter()
        if request.agent_name == "news_agent":
            data = self._news(request.input_payload)
        elif request.agent_name == "challenge_agent":
            data = self._challenge(request.input_payload)
        elif request.agent_name == "decision_manager":
            data = self._decision(request.input_payload)
        elif request.agent_name == "catalyst_candidate_extractor":
            data = self._catalyst_candidates(request.input_payload)
        elif request.agent_name in {"catalyst_ranker", "ai_gated_ranker"}:
            data = self._catalyst_ranking(request.input_payload)
        elif request.agent_name in {"catalyst_bull_news_agent", "ai_gated_news_agent"}:
            data = self._catalyst_bull_news(request.input_payload)
        elif request.agent_name in {"catalyst_challenge_agent", "ai_gated_challenge_agent"}:
            data = self._catalyst_challenge(request.input_payload)
        elif request.agent_name in {"catalyst_decision_manager", "ai_gated_decision_manager"}:
            data = self._catalyst_decision(request.input_payload)
        elif request.agent_name == "news_drift_headline_agent":
            data = self._news_drift_headlines(request.input_payload)
        else:
            raise ValueError(f"unknown mock agent: {request.agent_name}")
        validate_schema(data, request.output_schema)
        input_tokens = max(1, len(json.dumps(request.input_payload, sort_keys=True)) // 4)
        output_tokens = max(1, len(json.dumps(data, sort_keys=True)) // 4)
        latency_ms = (time.perf_counter() - started) * 1000
        usage = ProviderUsage(input_tokens, output_tokens, latency_ms, 0.0, 0)
        self.tracker.record(
            snapshot_id=str(request.input_payload["snapshot_id"]),
            agent_name=request.agent_name,
            provider="mock",
            model=self.model,
            prompt_version=request.prompt_version,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
        )
        return ProviderResponse(data, self.model, "mock", usage)

    @staticmethod
    def _news(payload: dict[str, Any]) -> dict[str, Any]:
        news = list(payload.get("available_news", []))
        events = []
        for item in news:
            events.append(
                {
                    "headline": str(item.get("headline", "")),
                    "published_at": str(item["published_at"]),
                    "first_seen_at": str(item["first_seen_at"]),
                    "source": str(item.get("source", "unknown")),
                    "source_tier": int(item.get("source_tier", 4)),
                    "ticker_relevance": float(item.get("ticker_relevance", 0)),
                    "direction": str(item.get("direction", "neutral")),
                    "novelty": float(item.get("novelty", 0)),
                    "already_priced_in": bool(item.get("already_priced_in", False)),
                    "confidence": float(item.get("confidence", 0.5)),
                }
            )
        if not events:
            return {
                "events": [],
                "published_at": None,
                "first_seen_at": None,
                "source": None,
                "source_tier": None,
                "ticker_relevance": 0.0,
                "direction": "none",
                "novelty": 0.0,
                "already_priced_in": False,
                "confidence": 0.0,
                "data_gaps": ["No grounded news event was available at the decision cutoff."],
            }
        primary = sorted(events, key=lambda item: (item["ticker_relevance"], item["novelty"], -item["source_tier"]), reverse=True)[0]
        sources = {str(item.get("source")) for item in payload.get("source_metadata", [])}
        gaps = []
        if primary["source"] not in sources:
            gaps.append("Primary event source is missing from source_metadata.")
        if primary["source_tier"] >= 3:
            gaps.append("Primary event is not supported by a high-tier source.")
        return {
            "events": events,
            "published_at": primary["published_at"],
            "first_seen_at": primary["first_seen_at"],
            "source": primary["source"],
            "source_tier": primary["source_tier"],
            "ticker_relevance": primary["ticker_relevance"],
            "direction": primary["direction"],
            "novelty": primary["novelty"],
            "already_priced_in": primary["already_priced_in"],
            "confidence": primary["confidence"],
            "data_gaps": gaps,
        }

    @staticmethod
    def _challenge(payload: dict[str, Any]) -> dict[str, Any]:
        context = payload.get("agent_context", {})
        news = context.get("news", {})
        technical = context.get("technical", {})
        objections: list[str] = []
        contradictions: list[str] = []
        missing = list(news.get("data_gaps", []))
        stale: list[str] = []
        direction = news.get("direction", "none")
        event_directions = {event.get("direction") for event in news.get("events", [])}
        if "positive" in event_directions and "negative" in event_directions:
            contradictions.append("Available sources conflict on event direction.")
        if direction in ("negative", "mixed"):
            objections.append("News direction is adverse or mixed.")
        if direction == "none":
            objections.append("No catalyst evidence supports a new entry.")
        if news.get("novelty", 0) < 0.4 and news.get("events"):
            stale.append("The primary event has low novelty and may be recycled.")
        if news.get("already_priced_in"):
            objections.append("The supplied event is marked already priced in.")
        price_change = float(technical.get("price_change_1d_pct", 0))
        if direction == "positive" and price_change < -1:
            contradictions.append("Positive catalyst conflicts with negative price confirmation.")
        chase_score = float(technical.get("chase_score", 0))
        chase_risk = "high" if chase_score >= 0.75 else ("medium" if chase_score >= 0.45 else "low")
        event_risk = "high" if bool(payload.get("market_data", {}).get("binary_event_within_days", 99) <= 2) else "low"
        if chase_risk == "high":
            objections.append("Price extension creates unacceptable chase risk.")
        if event_risk == "high":
            objections.append("A near-term binary event creates unacceptable gap risk.")
        veto = bool(
            direction in ("negative", "mixed", "none")
            or contradictions
            or stale
            or missing
            or chase_risk == "high"
            or event_risk == "high"
        )
        recommendation = "no_trade" if veto else ("reduce_confidence" if chase_risk == "medium" else "proceed")
        return {
            "objections": objections,
            "contradictions": contradictions,
            "missing_evidence": missing,
            "stale_evidence": stale,
            "chase_risk": chase_risk,
            "event_risk": event_risk,
            "recommendation": recommendation,
            "confidence_adjustment": -0.35 if veto else (-0.1 if recommendation == "reduce_confidence" else 0.0),
            "veto_recommended": veto,
        }

    @staticmethod
    def _decision(payload: dict[str, Any]) -> dict[str, Any]:
        context = payload.get("agent_context", {})
        news = context.get("news", {})
        challenge = context.get("challenge", {})
        technical = context.get("technical", {})
        regime = context.get("regime", {})
        ticker = str(payload["ticker"])
        has_position = bool(payload.get("market_data", {}).get("has_position", False))
        veto = bool(challenge.get("veto_recommended", False))
        positive = news.get("direction") == "positive" and float(news.get("confidence", 0)) >= 0.55
        candidate = bool(technical.get("candidate", False)) and regime.get("status") != "risk_off"
        if has_position and news.get("direction") == "negative":
            action = "exit"
        elif veto or not positive or not candidate:
            action = "no_trade"
        elif has_position:
            action = "hold"
        else:
            action = "buy"
        contrary = list(challenge.get("objections", [])) + list(challenge.get("contradictions", []))
        support = [event["headline"] for event in news.get("events", []) if event.get("direction") == "positive"]
        no_trade_reason = None
        if action == "no_trade":
            no_trade_reason = contrary[0] if contrary else "Minimum evidence and technical conditions were not met."
        confidence = max(0.0, min(1.0, float(news.get("confidence", 0)) + float(challenge.get("confidence_adjustment", 0))))
        return {
            "action": action,
            "ticker": ticker,
            "thesis": "Fresh grounded catalyst with relative-strength confirmation." if action in ("buy", "hold") else "No actionable evidence-backed entry is available.",
            "supporting_evidence": support,
            "contrary_evidence": contrary,
            "entry_condition": "Quote remains fresh, spread remains within policy, and deterministic risk gate approves.",
            "invalidation_condition": "Catalyst is contradicted, stale, or price confirmation fails.",
            "exit_condition": "Exit on invalidation, configured loss/profit rule, time stop, or pre-close flatten.",
            "confidence": round(confidence, 3),
            "max_holding_period": "5 trading days",
            "no_trade_reason": no_trade_reason,
        }

    @staticmethod
    def _catalyst_candidates(payload: dict[str, Any]) -> dict[str, Any]:
        candidates: list[dict[str, Any]] = []
        for index, seed in enumerate(payload.get("seed_candidates", [])[:20]):
            ticker = str(seed.get("ticker", "")).upper()
            if not ticker:
                continue
            candidates.append(
                {
                    "ticker": ticker,
                    "company_name": seed.get("company_name"),
                    "event_indices": [],
                    "discovery_score": round(max(0.1, 0.9 - index * 0.02), 3),
                    "reason": "deterministic seed candidate",
                }
            )
        return {"candidates": candidates, "data_gaps": [] if candidates else ["No seed candidate was available."]}

    @staticmethod
    def _catalyst_ranking(payload: dict[str, Any]) -> dict[str, Any]:
        ranked: list[dict[str, Any]] = []
        candidates = [item for item in payload.get("candidates", []) if item.get("eligible", False)]
        candidates.sort(key=lambda item: float(item.get("pre_score", 0)), reverse=True)
        for item in candidates[:8]:
            technical = item.get("market_context", {}).get("technical_signals", {})
            move = float(technical.get("price_change_1d_pct") or 0)
            direction = "bullish" if move >= 0 else "bearish"
            preference = "equity" if direction == "bullish" else "put"
            score = max(0.0, min(1.0, float(item.get("pre_score", 0.5))))
            ranked.append(
                {
                    "ticker": str(item["ticker"]),
                    "score": round(score, 3),
                    "direction": direction,
                    "catalyst_strength": round(score, 3),
                    "evidence_quality": 0.7,
                    "market_confirmation": round(min(1.0, abs(move) / 5), 3),
                    "instrument_preference": preference,
                    "rationale": "deterministic mock ranking",
                    "risk_flags": [],
                }
            )
        return {"ranked_candidates": ranked, "data_gaps": [] if ranked else ["No eligible candidate was available."]}

    @staticmethod
    def _catalyst_bull_news(payload: dict[str, Any]) -> dict[str, Any]:
        ticker = str(payload["ticker"])
        events = list(payload.get("available_news", []))
        primary = events[0] if events else {}
        explicit_event_time = primary.get("event_at")
        direction = str(primary.get("direction", "unclear"))
        if direction == "neutral":
            direction = "unclear"
        urls = [str(item.get("url")) for item in events if item.get("url")]
        return {
            "ticker": ticker,
            "catalyst_summary": str(primary.get("headline") or "No grounded catalyst."),
            "direction": direction if direction in {"positive", "negative", "mixed", "unclear"} else "unclear",
            "materiality": 0.8 if events else 0.0,
            "event_time": explicit_event_time,
            "event_time_basis": "source_explicit" if explicit_event_time else "unknown",
            "bull_case": "Grounded event may create a repricing opportunity." if events else "No bull case.",
            "supporting_facts": [str(item.get("headline")) for item in events if item.get("headline")],
            "source_urls": urls,
            "assumptions": [],
            "data_gaps": [] if events else ["No event evidence."],
            "already_priced_in": bool(primary.get("already_priced_in", False)),
            "confidence": 0.8 if events else 0.0,
            "instrument_preference": "equity" if direction == "positive" else ("put" if direction == "negative" else "none"),
        }

    @staticmethod
    def _catalyst_challenge(payload: dict[str, Any]) -> dict[str, Any]:
        bull = payload.get("agent_context", {}).get("bull_news", {})
        objections: list[str] = []
        missing = list(bull.get("data_gaps", []))
        if bull.get("direction") in {"mixed", "unclear"}:
            objections.append("Catalyst direction is not clear.")
        if bull.get("already_priced_in"):
            objections.append("Catalyst may already be priced in.")
        veto = bool(objections or missing or float(bull.get("confidence", 0)) < 0.55)
        return {
            "objections": objections,
            "contradictions": [],
            "missing_evidence": missing,
            "stale_evidence": [],
            "chase_risk": "low",
            "event_risk": "low",
            "recommendation": "no_trade" if veto else "proceed",
            "confidence_adjustment": -0.35 if veto else 0.0,
            "veto_recommended": veto,
        }

    @staticmethod
    def _catalyst_decision(payload: dict[str, Any]) -> dict[str, Any]:
        context = payload.get("agent_context", {})
        bull = context.get("bull_news", {})
        challenge = context.get("challenge", {})
        ticker = str(payload["ticker"])
        veto = bool(challenge.get("veto_recommended", False))
        preference = str(bull.get("instrument_preference", "none"))
        if veto or preference == "none":
            action = "no_trade"
            instrument = "none"
        elif preference == "equity":
            action = "buy"
            instrument = "equity"
        else:
            action = "buy_to_open"
            instrument = preference
        confidence = max(
            0.0,
            min(1.0, float(bull.get("confidence", 0)) + float(challenge.get("confidence_adjustment", 0))),
        )
        return {
            "action": action,
            "instrument": instrument,
            "ticker": ticker,
            "thesis": str(bull.get("catalyst_summary", "No actionable catalyst.")),
            "supporting_evidence": list(bull.get("supporting_facts", [])),
            "contrary_evidence": list(challenge.get("objections", [])),
            "entry_condition": "Fresh quote and deterministic risk approval.",
            "invalidation_condition": "Catalyst is contradicted or market confirmation reverses.",
            "exit_condition": "Configured stop, target, time stop, invalidation, or pre-close exit.",
            "confidence": round(confidence, 3),
            "max_holding_period": "5 trading days",
            "option_preference": {"target_dte": 30, "target_abs_delta": 0.45} if instrument in {"call", "put"} else None,
            "no_trade_reason": "Challenge veto or insufficient evidence." if action == "no_trade" else None,
        }

    @staticmethod
    def _news_drift_headlines(payload: dict[str, Any]) -> dict[str, Any]:
        signals: list[dict[str, Any]] = []
        recent = list(payload.get("recent_events", []))
        for index, event in enumerate(payload.get("events", [])):
            headline = str(event.get("headline", ""))
            lowered = headline.lower()
            ticker = event.get("ticker_hint")
            positive = any(word in lowered for word in ("raises", "beats", "approval", "wins", "acquires"))
            negative = any(word in lowered for word in ("cuts", "misses", "recall", "probe", "lawsuit", "rejects"))
            direction = "positive" if positive and not negative else ("negative" if negative and not positive else "unclear")
            related = next(
                (
                    item
                    for item in recent
                    if ticker and item.get("ticker") == ticker and str(item.get("headline", "")).lower() == lowered
                ),
                None,
            )
            signals.append(
                {
                    "event_index": index,
                    "ticker": str(ticker).upper() if ticker else None,
                    "company_name": event.get("company_name_hint"),
                    "direction": direction,
                    "event_type": "guidance" if "guidance" in lowered else ("regulatory" if "probe" in lowered else "other"),
                    "materiality": 0.8 if direction != "unclear" else 0.3,
                    "novelty": 0.0 if related else 0.8,
                    "ambiguity": 0.2 if direction != "unclear" else 0.8,
                    "relation_type": "duplicate" if related else "new_event",
                    "related_event_id": str(related["event_id"]) if related else None,
                    "confidence": 0.8 if ticker and direction != "unclear" else 0.3,
                    "rationale": "deterministic headline-only mock classification",
                }
            )
        return {"signals": signals, "data_gaps": [] if signals else ["No unseen headline was available."]}
