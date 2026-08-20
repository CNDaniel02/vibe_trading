from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

from scripts.core.models import parse_ts
from scripts.decision.signed_return_signal import validate_signed_return_signal
from scripts.llm.base_provider import LLMProvider, ProviderError, ProviderRequest
from scripts.llm.schemas import (
    AI_ALLOCATOR_RANKING_OUTPUT_SCHEMA,
    AI_ALLOCATOR_RESEARCH_OUTPUT_SCHEMA,
    AI_ALLOCATOR_SIGNAL_OUTPUT_SCHEMA,
    CHALLENGE_OUTPUT_SCHEMA,
    validate_agent_input,
)
from scripts.llm.usage_tracker import UsageTracker


_PROMPTS = {
    "ai_allocator_ranker": "ai_allocator_ranker.md",
    "ai_allocator_news_agent": "ai_allocator_news_agent.md",
    "ai_allocator_fast_news_agent": "ai_allocator_news_agent.md",
    "ai_allocator_challenge_agent": "ai_allocator_challenge_agent.md",
    "ai_allocator_fast_challenge_agent": "ai_allocator_challenge_agent.md",
    "ai_allocator_decision_manager": "ai_allocator_decision_manager.md",
    "ai_allocator_fast_decision_manager": "ai_allocator_decision_manager.md",
}


class AiInstrumentAllocatorTeam:
    STRATEGY = "ai_instrument_allocator_v1"

    def __init__(
        self,
        runtime_config: dict[str, Any],
        provider: LLMProvider,
        tracker: UsageTracker,
    ) -> None:
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
            "ai_allocator_ranker",
            {
                "snapshot_id": snapshot_id,
                "decision_time": decision_time,
                "data_cutoff_time": decision_time,
                "candidates": candidates,
            },
            AI_ALLOCATOR_RANKING_OUTPUT_SCHEMA,
        )

    def analyze(
        self,
        snapshot: dict[str, Any],
        ranking: dict[str, Any],
        *,
        stage: str,
    ) -> dict[str, Any]:
        calls_before = len(self.tracker.records)
        fast = stage != "overnight"
        names = {
            "news": "ai_allocator_fast_news_agent" if fast else "ai_allocator_news_agent",
            "challenge": (
                "ai_allocator_fast_challenge_agent"
                if fast
                else "ai_allocator_challenge_agent"
            ),
            "decision": (
                "ai_allocator_fast_decision_manager"
                if fast
                else "ai_allocator_decision_manager"
            ),
        }
        try:
            validate_agent_input(snapshot)
            news_payload = dict(snapshot)
            news_payload["agent_context"] = {"ranking": ranking, "stage": stage}
            news = self._call(
                names["news"],
                news_payload,
                AI_ALLOCATOR_RESEARCH_OUTPUT_SCHEMA,
            )
            challenge_payload = dict(snapshot)
            challenge_payload["agent_context"] = {
                "ranking": ranking,
                "bull_news": news,
                "stage": stage,
            }
            challenge = self._call(
                names["challenge"],
                challenge_payload,
                CHALLENGE_OUTPUT_SCHEMA,
            )
            decision_payload = dict(snapshot)
            decision_payload["agent_context"] = {
                "ranking": ranking,
                "bull_news": news,
                "challenge": challenge,
                "stage": stage,
            }
            signal = self._call(
                names["decision"],
                decision_payload,
                AI_ALLOCATOR_SIGNAL_OUTPUT_SCHEMA,
            )
            validate_signed_return_signal(signal)
        except (ProviderError, ValueError) as exc:
            return self._failed(snapshot, f"structured model failure: {exc}", calls_before)

        ticker = str(snapshot["ticker"])
        guardrails: list[str] = []
        allowed_urls = {
            str(item.get("url"))
            for item in snapshot.get("available_news", [])
            if item.get("url")
        }
        if news["ticker"] != ticker or signal["ticker"] != ticker:
            guardrails.append("model attempted to change immutable ticker")
            signal = self._no_trade(signal, ticker, "Model ticker did not match immutable input.")
        if set(news.get("source_urls", [])) - allowed_urls or set(
            signal.get("source_urls", [])
        ) - allowed_urls:
            guardrails.append("model cited evidence absent from immutable snapshot")
            signal = self._no_trade(signal, ticker, "Model cited unsupported evidence.")
        if challenge["veto_recommended"] and signal["action"] != "no_trade":
            guardrails.append("challenge veto enforced")
            signal = self._no_trade(signal, ticker, "Challenge veto is mandatory.")
        return {
            "strategy": self.STRATEGY,
            "snapshot_id": snapshot["snapshot_id"],
            "ticker": ticker,
            "stage": stage,
            "ranking": ranking,
            "bull_news": news,
            "challenge": challenge,
            "signal": signal,
            "model_calls": len(self.tracker.records) - calls_before,
            "guardrail_actions": guardrails,
            "fail_closed": False,
        }

    def _call(
        self,
        agent_name: str,
        payload: dict[str, Any],
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        prompt = (self.prompt_dir / _PROMPTS[agent_name]).read_text(encoding="utf-8")
        return self.provider.generate(
            ProviderRequest(
                agent_name=agent_name,
                prompt_version=self.prompt_version,
                system_prompt=prompt,
                input_payload=payload,
                output_schema=schema,
                schema_name=f"{agent_name}_{self.prompt_version}",
            )
        ).data

    def _failed(
        self,
        snapshot: dict[str, Any],
        reason: str,
        calls_before: int,
    ) -> dict[str, Any]:
        ticker = str(snapshot.get("ticker", "UNKNOWN"))
        return {
            "strategy": self.STRATEGY,
            "snapshot_id": str(snapshot.get("snapshot_id", "invalid")),
            "ticker": ticker,
            "stage": "failed",
            "ranking": {},
            "bull_news": None,
            "challenge": None,
            "signal": self._no_trade({}, ticker, reason),
            "model_calls": len(self.tracker.records) - calls_before,
            "guardrail_actions": ["pipeline failed closed"],
            "fail_closed": True,
        }

    @staticmethod
    def _no_trade(signal: dict[str, Any], ticker: str, reason: str) -> dict[str, Any]:
        decision_time = str(signal.get("thesis_valid_until") or "")
        if not decision_time:
            decision_time = (parse_ts("2026-01-01T00:00:00+00:00") + timedelta(days=1)).isoformat()
        return {
            **signal,
            "action": "no_trade",
            "ticker": ticker,
            "horizon": str(signal.get("horizon") or "next_close"),
            "signed_return_probability_buckets": dict(
                signal.get("signed_return_probability_buckets")
                or {
                    "return_lt_minus_5_pct": 0.0,
                    "return_minus_5_to_minus_2_pct": 0.0,
                    "return_minus_2_to_minus_0_5_pct": 0.0,
                    "return_minus_0_5_to_plus_0_5_pct": 1.0,
                    "return_plus_0_5_to_plus_2_pct": 0.0,
                    "return_plus_2_to_plus_5_pct": 0.0,
                    "return_gt_plus_5_pct": 0.0,
                }
            ),
            "probability_status": "uncalibrated",
            "thesis": str(signal.get("thesis") or "No evidence-backed entry."),
            "supporting_evidence": list(signal.get("supporting_evidence", [])),
            "source_urls": list(signal.get("source_urls", [])),
            "contrary_evidence": [*list(signal.get("contrary_evidence", [])), reason],
            "data_gaps": list(signal.get("data_gaps", [])),
            "entry_condition": "None.",
            "entry_now": False,
            "invalidation_condition": "Not applicable.",
            "thesis_valid_until": decision_time,
            "max_holding_trading_days": 0,
            "no_trade_reason": reason,
        }

