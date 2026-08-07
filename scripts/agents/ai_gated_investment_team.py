from __future__ import annotations

from pathlib import Path
from typing import Any

from scripts.llm.base_provider import LLMProvider, ProviderError, ProviderRequest
from scripts.llm.schemas import (
    CANDIDATE_RANKING_OUTPUT_SCHEMA,
    CATALYST_DECISION_OUTPUT_SCHEMA,
    CATALYST_RESEARCH_OUTPUT_SCHEMA,
    CHALLENGE_OUTPUT_SCHEMA,
    validate_agent_input,
)
from scripts.llm.usage_tracker import UsageTracker


PROMPTS = {
    "ai_gated_ranker": "ai_gated_ranker.md",
    "ai_gated_news_agent": "ai_gated_news_agent.md",
    "ai_gated_challenge_agent": "ai_gated_challenge_agent.md",
    "ai_gated_decision_manager": "ai_gated_decision_manager.md",
}


class AiGatedInvestmentTeam:
    """Provider-neutral model team for the isolated executable paper sleeve."""

    STRATEGY = "ai_gated_technical_v1"

    def __init__(self, runtime_config: dict[str, Any], provider: LLMProvider, tracker: UsageTracker) -> None:
        self.provider = provider
        self.tracker = tracker
        self.prompt_version = str(runtime_config.get("llm", {}).get("prompt_version", "v1"))
        self.prompt_dir = Path(__file__).resolve().parents[1] / "llm" / "prompts"

    def rank(
        self,
        *,
        snapshot_id: str,
        decision_time: str,
        candidates: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return self._call(
            "ai_gated_ranker",
            {
                "snapshot_id": snapshot_id,
                "decision_time": decision_time,
                "data_cutoff_time": decision_time,
                "candidates": candidates,
                "market_events": [],
            },
            CANDIDATE_RANKING_OUTPUT_SCHEMA,
        )

    def analyze(self, snapshot: dict[str, Any], ranking: dict[str, Any]) -> dict[str, Any]:
        calls_before = len(self.tracker.records)
        try:
            validate_agent_input(snapshot)
            news_payload = dict(snapshot)
            news_payload["agent_context"] = {"ranking": ranking}
            news = self._call("ai_gated_news_agent", news_payload, CATALYST_RESEARCH_OUTPUT_SCHEMA)
            challenge_payload = dict(snapshot)
            challenge_payload["agent_context"] = {"ranking": ranking, "bull_news": news}
            challenge = self._call("ai_gated_challenge_agent", challenge_payload, CHALLENGE_OUTPUT_SCHEMA)
            decision_payload = dict(snapshot)
            decision_payload["agent_context"] = {
                "ranking": ranking,
                "bull_news": news,
                "challenge": challenge,
            }
            decision = self._call("ai_gated_decision_manager", decision_payload, CATALYST_DECISION_OUTPUT_SCHEMA)
        except ProviderError as exc:
            return self._failed(snapshot, f"provider failure: {exc}", calls_before)
        except Exception as exc:
            return self._failed(snapshot, f"structured output failure: {type(exc).__name__}", calls_before)

        ticker = str(snapshot["ticker"])
        guardrails: list[str] = []
        if news["ticker"] != ticker or decision["ticker"] != ticker:
            guardrails.append("model attempted to change immutable ticker")
            decision = self._no_trade(decision, ticker, "Model ticker did not match immutable input.")
        allowed_urls = {str(item.get("url")) for item in snapshot["available_news"] if item.get("url")}
        if set(news.get("source_urls", [])) - allowed_urls:
            guardrails.append("model cited evidence absent from immutable snapshot")
            decision = self._no_trade(decision, ticker, "Model cited unsupported evidence.")
        if challenge["veto_recommended"] and decision["action"] != "no_trade":
            guardrails.append("challenge veto enforced")
            decision = self._no_trade(decision, ticker, "Challenge veto is mandatory.")
        if (decision.get("action"), decision.get("instrument")) not in {
            ("buy", "equity"),
            ("buy_to_open", "call"),
            ("buy_to_open", "put"),
            ("no_trade", "none"),
        }:
            guardrails.append("invalid action/instrument pair")
            decision = self._no_trade(decision, ticker, "Action and instrument were inconsistent.")
        return {
            "strategy": self.STRATEGY,
            "snapshot_id": snapshot["snapshot_id"],
            "ticker": ticker,
            "ranking": ranking,
            "bull_news": news,
            "challenge": challenge,
            "decision": decision,
            "model_calls": len(self.tracker.records) - calls_before,
            "guardrail_actions": guardrails,
            "fail_closed": False,
        }

    def _call(self, agent_name: str, payload: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
        prompt = (self.prompt_dir / PROMPTS[agent_name]).read_text(encoding="utf-8")
        response = self.provider.generate(
            ProviderRequest(
                agent_name=agent_name,
                prompt_version=self.prompt_version,
                system_prompt=prompt,
                input_payload=payload,
                output_schema=schema,
                schema_name=f"{agent_name}_{self.prompt_version}",
            )
        )
        return response.data

    def _failed(self, snapshot: dict[str, Any], reason: str, calls_before: int) -> dict[str, Any]:
        ticker = str(snapshot.get("ticker", "UNKNOWN"))
        return {
            "strategy": self.STRATEGY,
            "snapshot_id": str(snapshot.get("snapshot_id", "invalid")),
            "ticker": ticker,
            "ranking": {},
            "bull_news": None,
            "challenge": None,
            "decision": self._no_trade({}, ticker, reason),
            "model_calls": len(self.tracker.records) - calls_before,
            "guardrail_actions": ["pipeline failed closed"],
            "fail_closed": True,
        }

    @staticmethod
    def _no_trade(decision: dict[str, Any], ticker: str, reason: str) -> dict[str, Any]:
        return {
            **decision,
            "action": "no_trade",
            "instrument": "none",
            "ticker": ticker,
            "thesis": str(decision.get("thesis") or "No evidence-backed paper entry."),
            "supporting_evidence": list(decision.get("supporting_evidence", [])),
            "contrary_evidence": [*list(decision.get("contrary_evidence", [])), reason],
            "entry_condition": "None.",
            "invalidation_condition": "Not applicable.",
            "exit_condition": "Not applicable.",
            "confidence": 0.0,
            "max_holding_period": "0 trading days",
            "option_preference": None,
            "no_trade_reason": reason,
        }
