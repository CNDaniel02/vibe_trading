from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from scripts.agents.deterministic_agents import quote_from_snapshot, run_regime_agent, run_technical_agent
from scripts.core.models import Account, Order
from scripts.llm.base_provider import LLMProvider, ProviderError, ProviderRequest
from scripts.llm.schemas import CHALLENGE_OUTPUT_SCHEMA, DECISION_OUTPUT_SCHEMA, NEWS_OUTPUT_SCHEMA, validate_agent_input
from scripts.llm.usage_tracker import UsageTracker
from scripts.risk.risk_gate import check_order


PROMPT_FILES = {
    "news_agent": "news_agent.md",
    "challenge_agent": "challenge_agent.md",
    "decision_manager": "decision_manager.md",
}


@dataclass(frozen=True)
class ShadowTeamDecision:
    strategy: str
    snapshot_id: str
    ticker: str
    action: str
    regime: dict[str, Any]
    technical: dict[str, Any]
    news: dict[str, Any] | None
    challenge: dict[str, Any] | None
    decision: dict[str, Any]
    risk_approved: bool
    risk_reason: str
    model_calls: int
    fail_closed: bool
    guardrail_actions: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ApiInvestmentTeam:
    def __init__(
        self,
        root: str | Path,
        runtime_config: dict[str, Any],
        provider: LLMProvider,
        tracker: UsageTracker,
    ) -> None:
        self.root = Path(root)
        self.runtime_config = runtime_config
        self.provider = provider
        self.tracker = tracker
        self.prompt_version = str(runtime_config.get("llm", {}).get("prompt_version", "v1"))
        self.prompt_dir = Path(__file__).resolve().parents[1] / "llm" / "prompts"

    def run(self, snapshot: dict[str, Any]) -> ShadowTeamDecision:
        guardrails: list[str] = []
        try:
            validate_agent_input(snapshot)
        except Exception as exc:
            return self._fail_closed(snapshot, f"invalid or lookahead snapshot: {exc}", 0)

        regime = run_regime_agent(snapshot)
        technical = run_technical_agent(snapshot, self.runtime_config)
        if not regime["eligible"] or not technical["candidate"]:
            reason = "; ".join(regime["reasons"] or [technical["quote_reason"], "deterministic candidate threshold not met"])
            return self._deterministic_no_trade(snapshot, regime, technical, reason)

        calls_before = len(self.tracker.records)
        try:
            news = self._call("news_agent", snapshot, NEWS_OUTPUT_SCHEMA)
            if not news["events"]:
                return self._no_trade_after_calls(snapshot, regime, technical, news, None, "No grounded news event available.", calls_before)
            challenge_input = self._with_context(snapshot, {"regime": regime, "technical": technical, "news": news})
            challenge = self._call("challenge_agent", challenge_input, CHALLENGE_OUTPUT_SCHEMA)
            decision_input = self._with_context(
                snapshot,
                {"regime": regime, "technical": technical, "news": news, "challenge": challenge},
            )
            decision = self._call("decision_manager", decision_input, DECISION_OUTPUT_SCHEMA)
        except ProviderError as exc:
            return self._fail_closed(snapshot, f"provider failure: {exc}", len(self.tracker.records) - calls_before, regime, technical)
        except Exception as exc:
            return self._fail_closed(snapshot, f"structured output failure: {type(exc).__name__}", len(self.tracker.records) - calls_before, regime, technical)

        if decision["ticker"] != snapshot["ticker"]:
            guardrails.append("decision ticker was outside the immutable snapshot ticker")
            decision = dict(decision)
            decision["action"] = "no_trade"
            decision["ticker"] = snapshot["ticker"]
            decision["no_trade_reason"] = "Decision ticker did not match immutable snapshot ticker."
        if challenge["veto_recommended"] and decision["action"] == "buy":
            guardrails.append("challenge veto overrode decision manager buy")
            decision = dict(decision)
            decision["action"] = "no_trade"
            decision["no_trade_reason"] = "Challenge Agent recommended veto."

        risk_approved = False
        risk_reason = "no entry proposed"
        if decision["action"] == "buy":
            risk_approved, risk_reason = self._risk_check(snapshot, decision)
            if not risk_approved:
                guardrails.append("deterministic risk gate vetoed model decision")
                decision = dict(decision)
                decision["action"] = "no_trade"
                decision["no_trade_reason"] = f"Deterministic risk gate: {risk_reason}"

        return ShadowTeamDecision(
            strategy="multi_agent_relative_strength_v2_candidate",
            snapshot_id=snapshot["snapshot_id"],
            ticker=snapshot["ticker"],
            action=decision["action"],
            regime=regime,
            technical=technical,
            news=news,
            challenge=challenge,
            decision=decision,
            risk_approved=risk_approved,
            risk_reason=risk_reason,
            model_calls=len(self.tracker.records) - calls_before,
            fail_closed=False,
            guardrail_actions=guardrails,
        )

    def _call(self, name: str, payload: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
        prompt = (self.prompt_dir / PROMPT_FILES[name]).read_text(encoding="utf-8")
        response = self.provider.generate(
            ProviderRequest(
                agent_name=name,
                prompt_version=self.prompt_version,
                system_prompt=prompt,
                input_payload=self._compact_provider_payload(payload),
                output_schema=schema,
                schema_name=f"{name}_{self.prompt_version}",
            )
        )
        return response.data

    @staticmethod
    def _compact_provider_payload(payload: dict[str, Any]) -> dict[str, Any]:
        """Keep primary news facts but bound variable-length Exa excerpts.

        The immutable, full snapshot continues to be saved to the decision log.
        This only reduces the request transmitted to the LLM, avoiding slow or
        incomplete structured responses caused by unbounded search highlights.
        """
        result = dict(payload)
        news_items = payload.get("available_news")
        if not isinstance(news_items, list):
            return result

        allowed_fields = (
            "headline",
            "published_at",
            "first_seen_at",
            "source",
            "source_tier",
            "ticker_relevance",
            "direction",
            "novelty",
            "already_priced_in",
            "confidence",
            "url",
        )
        compacted: list[dict[str, Any]] = []
        for item in news_items[:6]:
            if not isinstance(item, dict):
                continue
            compact = {field: item[field] for field in allowed_fields if field in item}
            highlights = item.get("highlights")
            if isinstance(highlights, list):
                compact["highlights"] = [str(value)[:600] for value in highlights[:2]]
            elif isinstance(highlights, str):
                compact["highlights"] = highlights[:600]
            compacted.append(compact)
        result["available_news"] = compacted
        return result

    @staticmethod
    def _with_context(snapshot: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        result = dict(snapshot)
        result["agent_context"] = context
        return result

    def _risk_check(self, snapshot: dict[str, Any], decision: dict[str, Any]) -> tuple[bool, str]:
        quote = quote_from_snapshot(snapshot)
        if quote is None:
            return False, "missing quote"
        order = Order(
            order_id=f"shadow_{snapshot['snapshot_id']}",
            decision_id=f"shadow_{snapshot['snapshot_id']}",
            symbol=snapshot["ticker"],
            side="buy",
            order_type="limit",
            quantity=1.0,
            limit_price=quote.ask,
            quote_seen_at=quote.asof,
            idempotency_key=f"shadow_{snapshot['snapshot_id']}",
            thesis=decision["thesis"],
            created_at=snapshot["decision_time"],
        )
        shadow = snapshot.get("market_data", {}).get("shadow_account", {})
        initial_cash = float(shadow.get("initial_cash", 2000))
        account = Account(cash=float(shadow.get("cash", initial_cash)), initial_cash=initial_cash, updated_at=snapshot["decision_time"])
        risk = check_order(order, quote, account, {}, {}, {"date": snapshot["decision_time"][:10], "trades": 0}, self.runtime_config, snapshot["decision_time"])
        return risk.approved, risk.reason

    def _deterministic_no_trade(self, snapshot: dict[str, Any], regime: dict[str, Any], technical: dict[str, Any], reason: str) -> ShadowTeamDecision:
        decision = self._no_trade_payload(snapshot["ticker"], reason)
        return ShadowTeamDecision(
            "multi_agent_relative_strength_v2_candidate",
            snapshot["snapshot_id"],
            snapshot["ticker"],
            "no_trade",
            regime,
            technical,
            None,
            None,
            decision,
            False,
            reason,
            0,
            False,
            [],
        )

    def _no_trade_after_calls(
        self,
        snapshot: dict[str, Any],
        regime: dict[str, Any],
        technical: dict[str, Any],
        news: dict[str, Any],
        challenge: dict[str, Any] | None,
        reason: str,
        calls_before: int,
    ) -> ShadowTeamDecision:
        return ShadowTeamDecision(
            "multi_agent_relative_strength_v2_candidate",
            snapshot["snapshot_id"],
            snapshot["ticker"],
            "no_trade",
            regime,
            technical,
            news,
            challenge,
            self._no_trade_payload(snapshot["ticker"], reason),
            False,
            reason,
            len(self.tracker.records) - calls_before,
            False,
            [],
        )

    def _fail_closed(
        self,
        snapshot: dict[str, Any],
        reason: str,
        calls: int,
        regime: dict[str, Any] | None = None,
        technical: dict[str, Any] | None = None,
    ) -> ShadowTeamDecision:
        ticker = str(snapshot.get("ticker", "UNKNOWN"))
        return ShadowTeamDecision(
            "multi_agent_relative_strength_v2_candidate",
            str(snapshot.get("snapshot_id", "invalid")),
            ticker,
            "no_trade",
            regime or {"status": "unknown", "eligible": False, "reasons": [reason]},
            technical or {"candidate": False, "quote_valid": False, "quote_reason": reason},
            None,
            None,
            self._no_trade_payload(ticker, reason),
            False,
            reason,
            calls,
            True,
            ["pipeline failed closed"],
        )

    @staticmethod
    def _no_trade_payload(ticker: str, reason: str) -> dict[str, Any]:
        return {
            "action": "no_trade",
            "ticker": ticker,
            "thesis": "No evidence-backed trade proposal passed all gates.",
            "supporting_evidence": [],
            "contrary_evidence": [reason],
            "entry_condition": "None until a new valid snapshot is evaluated.",
            "invalidation_condition": "Not applicable.",
            "exit_condition": "Not applicable.",
            "confidence": 0.0,
            "max_holding_period": "0 trading days",
            "no_trade_reason": reason,
        }
