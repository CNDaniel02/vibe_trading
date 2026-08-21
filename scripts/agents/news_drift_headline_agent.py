from __future__ import annotations

from pathlib import Path
from typing import Any

from scripts.llm.base_provider import LLMProvider, ProviderRequest
from scripts.llm.schemas import NEWS_DRIFT_HEADLINE_OUTPUT_SCHEMA
from scripts.llm.usage_tracker import UsageTracker


class NewsDriftHeadlineAgent:
    """One fast, price-blind call for ticker mapping and event classification."""

    NAME = "news_drift_headline_agent"

    def __init__(self, config: dict[str, Any], provider: LLMProvider, tracker: UsageTracker) -> None:
        self.provider = provider
        self.tracker = tracker
        self.prompt_version = str(config.get("llm", {}).get("prompt_version", "v1"))
        self.prompt = (
            Path(__file__).resolve().parents[1]
            / "llm"
            / "prompts"
            / "news_drift_headline_agent.md"
        ).read_text(encoding="utf-8")

    def analyze(
        self,
        *,
        snapshot_id: str,
        decision_time: str,
        events: list[dict[str, Any]],
        recent_events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        payload = {
            "snapshot_id": snapshot_id,
            "decision_time": decision_time,
            "data_cutoff_time": decision_time,
            "events": [self._headline_only(item) for item in events[:20]],
            "recent_events": [self._recent_headline(item) for item in recent_events[:20]],
        }
        response = self.provider.generate(
            ProviderRequest(
                agent_name=self.NAME,
                prompt_version=self.prompt_version,
                system_prompt=self.prompt,
                input_payload=payload,
                output_schema=NEWS_DRIFT_HEADLINE_OUTPUT_SCHEMA,
                schema_name=f"{self.NAME}_{self.prompt_version}",
            )
        )
        signals: list[dict[str, Any]] = []
        seen: set[int] = set()
        for signal in response.data.get("signals", []):
            index = int(signal["event_index"])
            if index in seen or index < 0 or index >= len(payload["events"]):
                continue
            seen.add(index)
            signals.append(dict(signal))
        return {
            **response.data,
            "signals": signals,
            "provider": response.provider,
            "model": response.model,
        }

    @staticmethod
    def _headline_only(event: dict[str, Any]) -> dict[str, Any]:
        return {
            "headline": str(event.get("headline", ""))[:500],
            "published_at": event.get("published_at"),
            "published_at_precision": event.get("published_at_precision", "datetime"),
            "first_seen_at": event.get("first_seen_at"),
            "source": event.get("source"),
            "source_tier": event.get("source_tier"),
            "ticker_hint": event.get("ticker"),
            "company_name_hint": event.get("company_name"),
        }

    @staticmethod
    def _recent_headline(event: dict[str, Any]) -> dict[str, Any]:
        return {
            "event_id": event.get("event_id"),
            "ticker": event.get("ticker"),
            "headline": str(event.get("headline", ""))[:500],
            "published_at": event.get("published_at"),
        }
