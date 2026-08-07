from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

from scripts.adapters.errors import AdapterError
from scripts.adapters.exa_news_adapter import ExaNewsAdapter
from scripts.adapters.robinhood_discovery_adapter import RobinhoodDiscoveryAdapter
from scripts.adapters.robinhood_option_market_data_adapter import RobinhoodOptionMarketDataAdapter
from scripts.agents.catalyst_investment_team import CatalystInvestmentTeam
from scripts.core.audit import append_jsonl
from scripts.core.models import Order, Quote, parse_ts, utc_now
from scripts.discovery.evidence_store import EvidenceSnapshotStore
from scripts.discovery.catalyst_signal_store import CatalystSignalStore
from scripts.llm.base_provider import LLMProvider, ProviderError
from scripts.llm.usage_tracker import UsageTracker
from scripts.options.models import OptionOrder
from scripts.options.paper_broker import OptionPaperBroker
from scripts.options.risk_gate import check_option_order
from scripts.risk.position_sizing import calculate_entry_quantity
from scripts.risk.risk_gate import check_order
from scripts.runtime.market_clock import UsEquityMarketClock
from scripts.simulation.paper_broker import PaperBroker


class CatalystDiscoveryPipeline:
    """Independent Exa + DeepSeek catalyst lane with deterministic risk veto."""

    STRATEGY = "exa_deepseek_catalyst_v1"

    def __init__(
        self,
        root: str | Path,
        runtime_config: dict[str, Any],
        provider: LLMProvider,
        tracker: UsageTracker,
        *,
        discovery_adapter: RobinhoodDiscoveryAdapter | None = None,
        news_adapter: ExaNewsAdapter | None = None,
        option_data: RobinhoodOptionMarketDataAdapter | None = None,
    ) -> None:
        self.root = Path(root)
        self.config = runtime_config
        self.profile = runtime_config.get("strategies", {}).get(self.STRATEGY, {})
        self.discovery_config = dict(self.profile.get("discovery", {}))
        self.discovery_config["excluded_symbols"] = list(runtime_config.get("universe", {}).get("excluded_symbols", []))
        integrations = runtime_config.get("integrations", {})
        exa_config = dict(integrations.get("forward_data", {}).get("exa", {}))
        exa_config["lookback_hours"] = self.discovery_config.get("lookback_hours", exa_config.get("lookback_hours", 48))
        exa_config["max_market_searches"] = self.discovery_config.get("max_market_exa_searches", 2)
        self.discovery = discovery_adapter or RobinhoodDiscoveryAdapter(
            integrations.get("robinhood_mcp", {}),
            self.discovery_config,
            self.root,
        )
        self.news = news_adapter or ExaNewsAdapter(exa_config)
        self.option_data = option_data or RobinhoodOptionMarketDataAdapter(
            integrations.get("robinhood_mcp", {}),
            runtime_config,
            self.root,
        )
        self.team = CatalystInvestmentTeam(runtime_config, provider, tracker)
        self.tracker = tracker
        self.evidence = EvidenceSnapshotStore(self.root)
        self.signals = CatalystSignalStore(self.root)
        self.broker = PaperBroker(self.root, runtime_config)
        self.option_broker = OptionPaperBroker(self.root, runtime_config)
        self.clock = UsEquityMarketClock()

    def run(self, now: str | None = None) -> dict[str, Any]:
        decision_time = now or utc_now()
        clock = self.clock.status(decision_time)
        if not self.discovery_config.get("enabled", False):
            return {"event": "catalyst_discovery_skipped", "reason": "strategy disabled"}
        research_only = self._premarket_research_allowed(clock, decision_time)
        if not clock.is_regular and not research_only:
            return {
                "event": "catalyst_discovery_skipped",
                "reason": f"market session is {clock.market_session}",
                "clock": clock.to_dict(),
            }
        research_cutoff = int(
            self.discovery_config.get("minimum_minutes_to_close_for_research", 30)
        )
        if (
            clock.minutes_to_close is not None
            and clock.minutes_to_close <= research_cutoff
        ):
            return {
                "event": "catalyst_discovery_skipped",
                "reason": (
                    "insufficient time before market close for bounded research "
                    f"({clock.minutes_to_close:.1f}m <= {research_cutoff}m)"
                ),
                "clock": clock.to_dict(),
            }

        calls_before = len(self.tracker.records)
        cycle_id = f"cat_{uuid4().hex}"
        try:
            seeds = self.discovery.collect_seed_candidates(
                decision_time,
                list(self.config.get("universe", {}).get("default_watchlist", [])),
            )
            queries = list(self.discovery_config.get("market_queries", []))[
                : int(self.discovery_config.get("max_market_exa_searches", 2))
            ]
            market_events, market_sources = self.news.search_market_events(decision_time, queries)
            market_events = self.evidence.normalize_events(market_events)
            market_snapshot_time = decision_time if now is not None else utc_now()
            model_market_events = self.evidence.unsent_model_events(
                "market_discovery",
                market_events,
                market_snapshot_time,
                event_cooldown_hours=int(self.discovery_config.get("event_cooldown_hours", 24)),
            )
            raw_snapshot = self.evidence.write_snapshot(
                snapshot_type="market-discovery",
                decision_time=market_snapshot_time,
                payload={
                    "cycle_id": cycle_id,
                    "queries": queries,
                    "seed_candidates": seeds,
                    "events": market_events,
                    "source_metadata": market_sources,
                },
            )
            extracted = self.team.extract_candidates(
                snapshot_id=cycle_id,
                decision_time=market_snapshot_time,
                seed_candidates=seeds,
                market_events=model_market_events,
            )
            candidates = self._merge_candidates(seeds, extracted, model_market_events)
            candidate_symbols = [item["ticker"] for item in candidates]
            market_context = self.discovery.fetch_market_context(candidate_symbols, market_snapshot_time)
            ranked_input = self._ranked_input(candidates, market_context)
            ranking_time = market_snapshot_time if now is not None else utc_now()
            ranking = self.team.rank_candidates(
                snapshot_id=cycle_id,
                decision_time=ranking_time,
                candidates=ranked_input,
                market_events=model_market_events,
            )
            if not research_only:
                self.evidence.mark_model_events_sent(
                    "market_discovery",
                    model_market_events,
                    ranking_time,
                )
        except (AdapterError, ProviderError, RuntimeError, ValueError) as exc:
            event = {
                "event": "catalyst_discovery_failed_closed",
                "cycle_id": cycle_id,
                "stage": "candidate_discovery",
                "reason": f"{type(exc).__name__}: {exc}",
                "model_calls": len(self.tracker.records) - calls_before,
            }
            append_jsonl(self.root, "catalyst_discovery.jsonl", event)
            return event

        ranked = self._validated_ranking(ranking, market_context)
        deep_limit = int(self.discovery_config.get("max_deep_research_candidates", 3))
        ticker_search_limit = int(self.discovery_config.get("max_ticker_exa_searches", deep_limit))
        decisions: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for ranked_item in ranked[:deep_limit]:
            ticker = ranked_item["ticker"]
            if len(decisions) >= ticker_search_limit:
                break
            instrument = self.discovery.validate_instrument(ticker)
            if not instrument.get("valid", False):
                skipped.append({"ticker": ticker, "reason": instrument.get("reason", "instrument validation failed")})
                continue
            company_name = instrument.get("name")
            try:
                events, sources = self.news.search(ticker, utc_now() if now is None else now, company_name=company_name)
            except AdapterError as exc:
                skipped.append({"ticker": ticker, "reason": f"Exa failed closed: {exc}"})
                continue
            events = self.evidence.normalize_events(events, ticker=ticker)
            evidence_snapshot = self.evidence.write_snapshot(
                snapshot_type=f"ticker-{ticker}",
                decision_time=utc_now() if now is None else now,
                payload={
                    "cycle_id": cycle_id,
                    "ticker": ticker,
                    "instrument": instrument,
                    "ranking": ranked_item,
                    "events": events,
                    "source_metadata": sources,
                    "market_context": market_context[ticker],
                },
            )
            eligible, model_events, reason = self.evidence.research_eligibility(
                ticker,
                events,
                utc_now() if now is None else now,
                ticker_cooldown_minutes=int(self.discovery_config.get("ticker_cooldown_minutes", 120)),
                event_cooldown_hours=int(self.discovery_config.get("event_cooldown_hours", 24)),
            )
            if not eligible:
                skipped.append({"ticker": ticker, "reason": reason, "evidence_snapshot": evidence_snapshot})
                continue
            snapshot = self._agent_snapshot(
                ticker=ticker,
                decision_time=utc_now() if now is None else now,
                market_context=market_context[ticker],
                events=model_events,
                sources=sources,
                market_session=clock.market_session,
            )
            analysis = self.team.analyze(snapshot, ranked_item)
            analysis["evidence_snapshot"] = evidence_snapshot
            analysis.update(
                self._apply_deterministic_risk(
                    analysis,
                    snapshot,
                    risk_now=snapshot["decision_time"] if now is not None else None,
                    research_only=research_only,
                )
            )
            append_jsonl(self.root, "catalyst_decisions.jsonl", analysis)
            bull = analysis.get("bull_news")
            if isinstance(bull, dict) and not research_only:
                source_tiers = [
                    int(event.get("source_tier", 4))
                    for event in model_events
                    if event.get("source_tier") is not None
                ]
                self.signals.put(
                    ticker,
                    {
                        "strategy": self.STRATEGY,
                        "direction": bull.get("direction"),
                        "confidence": bull.get("confidence", 0),
                        "materiality": bull.get("materiality", 0),
                        "source_tier": min(source_tiers, default=4),
                        "catalyst_summary": bull.get("catalyst_summary"),
                    },
                    observed_at=snapshot["decision_time"],
                )
            decisions.append(analysis)
            if not research_only:
                self.evidence.mark_researched(
                    ticker,
                    model_events,
                    snapshot["decision_time"],
                )

        result = {
            "event": "catalyst_discovery_complete",
            "strategy": self.STRATEGY,
            "execution": self.profile.get("execution", "shadow_only"),
            "cycle_id": cycle_id,
            "decision_time": decision_time,
            "seed_candidate_count": len(seeds),
            "market_event_count": len(market_events),
            "market_events_sent_to_model": len(model_market_events),
            "candidate_count": len(candidates),
            "eligible_market_context_count": len(market_context),
            "ranked_candidates": ranked,
            "decisions": decisions,
            "skipped_deep_research": skipped,
            "market_snapshot": raw_snapshot,
            "model_calls": len(self.tracker.records) - calls_before,
            "usage": self.tracker.summary(),
            "research_only": research_only,
            "paper_orders_created": 0,
            "live_order_tools_called": False,
        }
        append_jsonl(self.root, "catalyst_discovery.jsonl", result)
        return result

    def _premarket_research_allowed(
        self,
        clock: Any,
        decision_time: str,
    ) -> bool:
        if (
            clock.market_session != "pre_market"
            or not self.discovery_config.get(
                "allow_premarket_research",
                False,
            )
            or not clock.open_time
        ):
            return False
        minutes_to_open = (
            parse_ts(clock.open_time) - parse_ts(decision_time)
        ).total_seconds() / 60
        window = float(
            self.discovery_config.get(
                "premarket_research_window_minutes",
                90,
            )
        )
        return 0 < minutes_to_open <= window

    def _merge_candidates(
        self,
        seeds: list[dict[str, Any]],
        extracted: dict[str, Any],
        market_events: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        for seed in seeds:
            ticker = str(seed.get("ticker", "")).upper()
            if not ticker:
                continue
            merged[ticker] = {
                **seed,
                "ticker": ticker,
                "discovery_score": 0.4,
                "event_indices": [],
                "discovery_reason": "deterministic seed source",
            }
        for item in extracted.get("candidates", []):
            ticker = str(item.get("ticker", "")).upper()
            if not ticker:
                continue
            candidate = merged.setdefault(ticker, {"ticker": ticker, "sources": ["exa_model_extraction"], "source_details": []})
            candidate["company_name"] = item.get("company_name")
            candidate["discovery_score"] = float(item.get("discovery_score", 0))
            candidate["event_indices"] = [
                int(index)
                for index in item.get("event_indices", [])
                if isinstance(index, int) and 0 <= index < len(market_events)
            ]
            candidate["discovery_reason"] = str(item.get("reason", ""))
        candidates = list(merged.values())
        candidates.sort(key=lambda item: float(item.get("discovery_score", 0)), reverse=True)
        return candidates[: int(self.discovery_config.get("max_candidates_before_ranking", 30))]

    def _ranked_input(
        self,
        candidates: list[dict[str, Any]],
        market_context: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for item in candidates:
            ticker = item["ticker"]
            context = market_context.get(ticker)
            if context is None:
                continue
            technical = context.get("technical_signals", {})
            source_details = item.get("source_details", [])
            earnings_surprises = [
                detail.get("detail", {}).get("eps_surprise_ratio")
                for detail in source_details
                if detail.get("source") == "earnings_calendar"
            ]
            surprise = max((abs(float(value)) for value in earnings_surprises if value is not None), default=0.0)
            volume_ratio = abs(float(technical.get("volume_ratio") or 0))
            move_1d = abs(float(technical.get("price_change_1d_pct") or 0))
            rs20 = abs(float(technical.get("relative_strength_20d") or 0))
            pre_score = (
                0.25 * float(item.get("discovery_score", 0))
                + 0.25 * min(1.0, volume_ratio / 2)
                + 0.20 * min(1.0, move_1d / 5)
                + 0.15 * min(1.0, rs20 / 10)
                + 0.15 * min(1.0, surprise)
            )
            result.append(
                {
                    **item,
                    "eligible": bool(context.get("eligible", False)),
                    "pre_score": round(pre_score, 4),
                    "market_context": context,
                }
            )
        result.sort(key=lambda item: item["pre_score"], reverse=True)
        return result

    def _validated_ranking(
        self,
        ranking: dict[str, Any],
        market_context: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        validated: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in ranking.get("ranked_candidates", []):
            ticker = str(item.get("ticker", "")).upper()
            if ticker in seen or ticker not in market_context or not market_context[ticker].get("eligible", False):
                continue
            seen.add(ticker)
            validated.append({**item, "ticker": ticker})
        validated.sort(key=lambda item: float(item.get("score", 0)), reverse=True)
        return validated[: int(self.discovery_config.get("max_ranked_candidates", 8))]

    def _agent_snapshot(
        self,
        *,
        ticker: str,
        decision_time: str,
        market_context: dict[str, Any],
        events: list[dict[str, Any]],
        sources: list[dict[str, Any]],
        market_session: str = "regular",
    ) -> dict[str, Any]:
        quote = dict(market_context["quote"])
        return {
            "snapshot_id": f"catalyst_{ticker}_{uuid4().hex}",
            "decision_time": decision_time,
            "data_cutoff_time": decision_time,
            "ticker": ticker,
            "market_session": market_session,
            "market_data": {
                "quote": quote,
                "fundamentals": market_context.get("fundamentals", {}),
                "market_regime": "neutral",
                "has_position": ticker in self.broker.store.positions(),
                "shadow_account": self.broker.store.account().to_dict(),
            },
            "technical_signals": market_context.get("technical_signals", {}),
            "available_news": events,
            "source_metadata": [
                {
                    "source": "robinhood_mcp",
                    "source_tier": 1,
                    "retrieved_at": decision_time,
                },
                *sources,
            ],
        }

    def _apply_deterministic_risk(
        self,
        analysis: dict[str, Any],
        snapshot: dict[str, Any],
        *,
        risk_now: str | None = None,
        research_only: bool = False,
    ) -> dict[str, Any]:
        decision = analysis["decision"]
        if decision["action"] == "no_trade":
            return {
                "risk_approved": False,
                "risk_reason": decision.get("no_trade_reason") or "no entry proposed",
                "proposal": None,
                "final_action": "no_trade",
            }
        if research_only:
            return {
                "risk_approved": False,
                "risk_reason": (
                    "premarket research only; regular-session quote and "
                    "deterministic risk revalidation are required"
                ),
                "proposal": {
                    "instrument": decision.get("instrument"),
                    "ticker": snapshot["ticker"],
                    "planned_action": decision.get("action"),
                },
                "final_action": "no_trade",
                "research_only": True,
            }
        ticker = snapshot["ticker"]
        observed_quote = Quote(**snapshot["market_data"]["quote"])
        try:
            quote = self.discovery.fetch_current_quote(
                ticker,
                average_daily_volume_usd=observed_quote.avg_daily_volume_usd,
                asset_class=observed_quote.asset_class,
            )
        except Exception as exc:
            return {
                "risk_approved": False,
                "risk_reason": f"quote refresh failed closed: {type(exc).__name__}",
                "proposal": None,
                "final_action": "no_trade",
            }
        account = self.broker.store.account()
        positions = self.broker.store.positions()
        equity_orders = self.broker.store.orders()
        option_positions = self.option_broker.store.positions()
        option_orders = self.option_broker.store.orders()
        counters = self.broker.store.daily_counters()
        evaluated_at = risk_now or utc_now()

        if decision["instrument"] == "equity":
            quantity = calculate_entry_quantity(
                account,
                positions,
                quote,
                self.config["risk"],
                notional_buffer_pct=float(
                    self.config.get("integrations", {}).get("runtime", {}).get("order_notional_buffer_pct", 0.96)
                ),
            )
            order = Order(
                order_id=f"shadow_catalyst_{uuid4().hex}",
                decision_id=snapshot["snapshot_id"],
                symbol=ticker,
                side="buy",
                order_type="limit",
                quantity=quantity,
                limit_price=quote.ask,
                quote_seen_at=quote.asof,
                idempotency_key=f"{self.STRATEGY}:{snapshot['snapshot_id']}:equity",
                thesis=decision["thesis"],
                created_at=evaluated_at,
            )
            risk = check_order(
                order,
                quote,
                account,
                positions,
                equity_orders,
                counters,
                self.config,
                evaluated_at,
                option_positions=option_positions,
                option_orders=option_orders,
            )
            proposal = {
                "instrument": "equity",
                "ticker": ticker,
                "quantity": quantity,
                "limit_price": quote.ask,
                "quote_seen_at": quote.asof,
                "evaluated_at": evaluated_at,
            }
        else:
            account_equity_at_cost = account.cash
            account_equity_at_cost += sum(position.average_price * position.quantity for position in positions.values())
            account_equity_at_cost += sum(position.cost_basis() for position in option_positions.values())
            premium_budget = account_equity_at_cost * float(
                self.config["options_risk"].get("max_order_risk_pct_of_equity", 0.10)
            ) * 0.95
            try:
                selected = self.option_data.fetch_best_contract(
                    underlying=ticker,
                    underlying_price=quote.last,
                    option_type=decision["instrument"],
                    now=evaluated_at,
                    max_premium_usd=premium_budget,
                )
            except Exception as exc:
                return {
                    "risk_approved": False,
                    "risk_reason": f"option contract selection failed closed: {type(exc).__name__}",
                    "proposal": None,
                    "final_action": "no_trade",
                }
            if selected is None:
                return {
                    "risk_approved": False,
                    "risk_reason": "no liquid option contract passed deterministic selection",
                    "proposal": None,
                    "final_action": "no_trade",
                }
            contract, option_quote = selected
            option_evaluated_at = risk_now or utc_now()
            option_order = OptionOrder(
                order_id=f"shadow_catalyst_option_{uuid4().hex}",
                decision_id=snapshot["snapshot_id"],
                contract=contract,
                intent="buy_to_open",
                quantity=1,
                order_type="limit",
                limit_price=option_quote.ask,
                quote_seen_at=option_quote.updated_at,
                idempotency_key=f"{self.STRATEGY}:{snapshot['snapshot_id']}:{contract.option_id}",
                thesis=decision["thesis"],
                created_at=option_evaluated_at,
            )
            risk = check_option_order(
                option_order,
                option_quote,
                account,
                positions,
                option_positions,
                equity_orders,
                option_orders,
                counters,
                self.config,
                option_evaluated_at,
            )
            proposal = {
                "instrument": decision["instrument"],
                "ticker": ticker,
                "contract": contract.to_dict(),
                "quantity": 1,
                "limit_price": option_quote.ask,
                "quote_seen_at": option_quote.updated_at,
                "evaluated_at": option_evaluated_at,
            }
        return {
            "risk_approved": risk.approved,
            "risk_reason": risk.reason,
            "proposal": proposal,
            "final_action": decision["action"] if risk.approved else "no_trade",
        }
