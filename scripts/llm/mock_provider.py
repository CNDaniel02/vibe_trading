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
