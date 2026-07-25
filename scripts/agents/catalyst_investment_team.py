from __future__ import annotations

from pathlib import Path
from typing import Any

from scripts.llm.base_provider import LLMProvider, ProviderError, ProviderRequest
from scripts.llm.schemas import (
    CANDIDATE_EXTRACTOR_OUTPUT_SCHEMA,
    CANDIDATE_RANKING_OUTPUT_SCHEMA,
    CATALYST_DECISION_OUTPUT_SCHEMA,
    CATALYST_RESEARCH_OUTPUT_SCHEMA,
    CHALLENGE_OUTPUT_SCHEMA,
    validate_agent_input,
)
from scripts.llm.usage_tracker import UsageTracker


PROMPT_FILES = {
    "catalyst_candidate_extractor": "catalyst_candidate_extractor.md",
    "catalyst_ranker": "catalyst_ranker.md",
    "catalyst_bull_news_agent": "catalyst_bull_news_agent.md",
    "catalyst_challenge_agent": "catalyst_challenge_agent.md",
    "catalyst_decision_manager": "catalyst_decision_manager.md",
}


class CatalystInvestmentTeam:
    """Provider-neutral independent catalyst research and proposal team."""

    def __init__(
        self,
        runtime_config: dict[str, Any],
        provider: LLMProvider,
        tracker: UsageTracker,
    ) -> None:
        self.runtime_config = runtime_config
        self.provider = provider
        self.tracker = tracker
        self.prompt_version = str(runtime_config.get("llm", {}).get("prompt_version", "v1"))
        self.prompt_dir = Path(__file__).resolve().parents[1] / "llm" / "prompts"

    def extract_candidates(
        self,
        *,
        snapshot_id: str,
        decision_time: str,
        seed_candidates: list[dict[str, Any]],
        market_events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        payload = {
            "snapshot_id": snapshot_id,
            "decision_time": decision_time,
            "data_cutoff_time": decision_time,
            "seed_candidates": seed_candidates,
            "market_events": self._compact_events(market_events, maximum=12),
        }
        return self._call("catalyst_candidate_extractor", payload, CANDIDATE_EXTRACTOR_OUTPUT_SCHEMA)

    def rank_candidates(
        self,
        *,
        snapshot_id: str,
        decision_time: str,
        candidates: list[dict[str, Any]],
        market_events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        payload = {
            "snapshot_id": snapshot_id,
            "decision_time": decision_time,
            "data_cutoff_time": decision_time,
            "candidates": candidates,
            "market_events": self._compact_events(market_events, maximum=12),
        }
        return self._call("catalyst_ranker", payload, CANDIDATE_RANKING_OUTPUT_SCHEMA)

    def analyze(self, snapshot: dict[str, Any], ranking: dict[str, Any]) -> dict[str, Any]:
        calls_before = len(self.tracker.records)
        guardrails: list[str] = []
        try:
            validate_agent_input(snapshot)
            bull_payload = dict(snapshot)
            bull_payload["agent_context"] = {"ranking": ranking}
            bull = self._call("catalyst_bull_news_agent", bull_payload, CATALYST_RESEARCH_OUTPUT_SCHEMA)
            challenge_payload = dict(snapshot)
            challenge_payload["agent_context"] = {"ranking": ranking, "bull_news": bull}
            challenge = self._call("catalyst_challenge_agent", challenge_payload, CHALLENGE_OUTPUT_SCHEMA)
            decision_payload = dict(snapshot)
            decision_payload["agent_context"] = {
                "ranking": ranking,
                "bull_news": bull,
                "challenge": challenge,
            }
            decision = self._call("catalyst_decision_manager", decision_payload, CATALYST_DECISION_OUTPUT_SCHEMA)
        except ProviderError as exc:
            return self._failed(snapshot, f"provider failure: {exc}", calls_before)
        except Exception as exc:
            return self._failed(snapshot, f"structured output failure: {type(exc).__name__}", calls_before)

        ticker = str(snapshot["ticker"])
        if bull["ticker"] != ticker or decision["ticker"] != ticker:
            guardrails.append("model attempted to change immutable ticker")
            decision = self._force_no_trade(decision, ticker, "Model ticker did not match the immutable snapshot.")
        supplied_urls = {str(item.get("url")) for item in snapshot.get("available_news", []) if item.get("url")}
        unsupported_urls = sorted(set(bull.get("source_urls", [])) - supplied_urls)
        if unsupported_urls:
            guardrails.append("bull agent cited URLs absent from immutable evidence")
            decision = self._force_no_trade(decision, ticker, "Bull analysis cited unsupported source URLs.")
        if challenge["veto_recommended"] and decision["action"] != "no_trade":
            guardrails.append("challenge veto overrode decision manager")
            decision = self._force_no_trade(decision, ticker, "Challenge Agent recommended veto.")
        if not self._instrument_action_consistent(decision):
            guardrails.append("decision action and instrument were inconsistent")
            decision = self._force_no_trade(decision, ticker, "Decision action and instrument were inconsistent.")

        return {
            "strategy": "exa_deepseek_catalyst_v1",
            "snapshot_id": snapshot["snapshot_id"],
            "ticker": ticker,
            "ranking": ranking,
            "bull_news": bull,
            "challenge": challenge,
            "decision": decision,
            "action": decision["action"],
            "instrument": decision["instrument"],
            "model_calls": len(self.tracker.records) - calls_before,
            "fail_closed": False,
            "guardrail_actions": guardrails,
        }

    def _call(self, name: str, payload: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
        prompt = (self.prompt_dir / PROMPT_FILES[name]).read_text(encoding="utf-8")
        response = self.provider.generate(
            ProviderRequest(
                agent_name=name,
                prompt_version=self.prompt_version,
                system_prompt=prompt,
                input_payload=payload,
                output_schema=schema,
                schema_name=f"{name}_{self.prompt_version}",
            )
        )
        return response.data

    @staticmethod
    def _compact_events(events: list[dict[str, Any]], maximum: int) -> list[dict[str, Any]]:
        allowed = (
            "ticker",
            "headline",
            "published_at",
            "event_at",
            "first_seen_at",
            "retrieved_at",
            "source",
            "source_tier",
            "url",
            "event_fingerprint",
            "content_hash",
        )
        compacted: list[dict[str, Any]] = []
        for event in events[:maximum]:
            compact = {key: event[key] for key in allowed if key in event}
            highlights = event.get("highlights")
            if isinstance(highlights, list):
                compact["highlights"] = [str(value)[:500] for value in highlights[:2]]
            elif isinstance(highlights, str):
                compact["highlights"] = highlights[:500]
            compacted.append(compact)
        return compacted

    def _failed(self, snapshot: dict[str, Any], reason: str, calls_before: int) -> dict[str, Any]:
        ticker = str(snapshot.get("ticker", "UNKNOWN"))
        return {
            "strategy": "exa_deepseek_catalyst_v1",
            "snapshot_id": str(snapshot.get("snapshot_id", "invalid")),
            "ticker": ticker,
            "ranking": {},
            "bull_news": None,
            "challenge": None,
            "decision": {
                "action": "no_trade",
                "instrument": "none",
                "ticker": ticker,
                "thesis": "Catalyst pipeline failed closed.",
                "supporting_evidence": [],
                "contrary_evidence": [reason],
                "entry_condition": "None.",
                "invalidation_condition": "Not applicable.",
                "exit_condition": "Not applicable.",
                "confidence": 0.0,
                "max_holding_period": "0 trading days",
                "option_preference": None,
                "no_trade_reason": reason,
            },
            "action": "no_trade",
            "instrument": "none",
            "model_calls": len(self.tracker.records) - calls_before,
            "fail_closed": True,
            "guardrail_actions": ["pipeline failed closed"],
        }

    @staticmethod
    def _force_no_trade(decision: dict[str, Any], ticker: str, reason: str) -> dict[str, Any]:
        result = dict(decision)
        result.update(
            {
                "action": "no_trade",
                "instrument": "none",
                "ticker": ticker,
                "option_preference": None,
                "no_trade_reason": reason,
            }
        )
        return result

    @staticmethod
    def _instrument_action_consistent(decision: dict[str, Any]) -> bool:
        pair = (decision.get("action"), decision.get("instrument"))
        return pair in {
            ("buy", "equity"),
            ("buy_to_open", "call"),
            ("buy_to_open", "put"),
            ("no_trade", "none"),
        }
