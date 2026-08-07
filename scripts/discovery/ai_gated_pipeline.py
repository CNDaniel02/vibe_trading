from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from uuid import uuid4

from scripts.adapters.errors import AdapterError
from scripts.adapters.exa_news_adapter import ExaNewsAdapter
from scripts.adapters.robinhood_discovery_adapter import RobinhoodDiscoveryAdapter
from scripts.adapters.robinhood_option_market_data_adapter import RobinhoodOptionMarketDataAdapter
from scripts.agents.ai_gated_investment_team import AiGatedInvestmentTeam
from scripts.core.audit import append_jsonl
from scripts.core.models import Quote, parse_ts, utc_now
from scripts.discovery.catalyst_signal_store import CatalystSignalStore
from scripts.discovery.evidence_store import EvidenceSnapshotStore
from scripts.exit.evaluate_exit import evaluate_position_exit
from scripts.journal.write_trade_journal import write_order_journal
from scripts.llm.base_provider import LLMProvider, ProviderError
from scripts.llm.usage_tracker import UsageTracker
from scripts.options.paper_broker import OptionPaperBroker
from scripts.options.exit_policy import evaluate_option_exit
from scripts.risk.position_sizing import calculate_entry_quantity
from scripts.risk.shared_portfolio_risk import daily_entry_limit_reason
from scripts.runtime.market_clock import UsEquityMarketClock
from scripts.simulation.paper_broker import PaperBroker
from scripts.strategies.technical_scoring import directional_feature_scores, weighted_score, DEFAULT_WEIGHTS


class AiGatedPaperPipeline:
    """Technical top-set -> Exa -> DeepSeek -> deterministic-risk paper sleeve."""

    STRATEGY = "ai_gated_technical_v1"

    def __init__(
        self,
        root: str | Path,
        config: dict[str, Any],
        provider: LLMProvider,
        tracker: UsageTracker,
        *,
        discovery_adapter: RobinhoodDiscoveryAdapter | None = None,
        news_adapter: ExaNewsAdapter | None = None,
        option_data: RobinhoodOptionMarketDataAdapter | None = None,
    ) -> None:
        self.root = Path(root)
        self.config = config
        self.profile = config.get("strategies", {}).get(self.STRATEGY, {})
        self.namespace = str(self.profile.get("state_namespace", self.STRATEGY))
        integrations = config.get("integrations", {})
        discovery_config = {
            **config.get("strategies", {}).get("exa_deepseek_catalyst_v1", {}).get("discovery", {}),
            "excluded_symbols": config.get("universe", {}).get("excluded_symbols", []),
        }
        self.discovery = discovery_adapter or RobinhoodDiscoveryAdapter(
            integrations.get("robinhood_mcp", {}),
            discovery_config,
            self.root,
        )
        self.news = news_adapter or ExaNewsAdapter(integrations.get("forward_data", {}).get("exa", {}))
        self.option_data = option_data or RobinhoodOptionMarketDataAdapter(
            integrations.get("robinhood_mcp", {}),
            config,
            self.root,
        )
        self.team = AiGatedInvestmentTeam(config, provider, tracker)
        self.tracker = tracker
        self.evidence = EvidenceSnapshotStore(self.root)
        self.signals = CatalystSignalStore(self.root)
        self.broker = PaperBroker(self.root, config, namespace=self.namespace)
        self.option_broker = OptionPaperBroker(self.root, config, namespace=self.namespace)
        self.clock = UsEquityMarketClock()

    def run(self, now: str | None = None) -> dict[str, Any]:
        decision_time = now or utc_now()
        clock = self.clock.status(decision_time)
        if not self.profile.get("enabled", False):
            return {"event": "ai_gated_cycle_skipped", "reason": "strategy disabled"}
        research_only = self._premarket_research_allowed(clock, decision_time)
        if not clock.is_regular and not research_only:
            return {
                "event": "ai_gated_cycle_skipped",
                "reason": f"market session is {clock.market_session}",
                "clock": clock.to_dict(),
            }

        cycle_id = f"aigt_{uuid4().hex}"
        calls_before = len(self.tracker.records)
        monitor = self.monitor_only(decision_time)
        research_cutoff = int(
            self.profile.get("minimum_minutes_to_close_for_research", 30)
        )
        if (
            clock.minutes_to_close is not None
            and clock.minutes_to_close <= research_cutoff
        ):
            return {
                "event": "ai_gated_cycle_skipped",
                "cycle_id": cycle_id,
                "reason": (
                    "insufficient time before market close for bounded research "
                    f"({clock.minutes_to_close:.1f}m <= {research_cutoff}m)"
                ),
                "clock": clock.to_dict(),
                "monitor": monitor,
                "model_calls": 0,
                "paper_orders_created": 0,
                "paper_sleeve": self.namespace,
                "live_order_tools_called": False,
            }
        if not research_only:
            entry_lines = []
            if self.profile.get("allow_equity", True):
                entry_lines.append("equity")
            if self.config.get("paper", {}).get("strategy_lines", {}).get("options", False) and (
                self.profile.get("allow_long_call", False)
                or self.profile.get("allow_long_put", False)
            ):
                entry_lines.append("options")
            entry_blocks = {
                line: self._entry_block_reason(line, decision_time)
                for line in entry_lines
            }
            if entry_blocks and all(entry_blocks.values()):
                reasons = sorted(set(str(reason) for reason in entry_blocks.values()))
                reason = reasons[0] if len(reasons) == 1 else "; ".join(reasons)
                return {
                    "event": "ai_gated_cycle_skipped",
                    "cycle_id": cycle_id,
                    "reason": reason,
                    "entry_blocks": entry_blocks,
                    "clock": clock.to_dict(),
                    "monitor": monitor,
                    "model_calls": 0,
                    "paper_orders_created": 0,
                    "paper_sleeve": self.namespace,
                    "live_order_tools_called": False,
                }
        try:
            seeds = self.discovery.collect_seed_candidates(
                decision_time,
                list(self.config.get("universe", {}).get("default_watchlist", [])),
            )
            seed_by_ticker = {str(item["ticker"]).upper(): item for item in seeds if item.get("ticker")}
            contexts = self.discovery.fetch_market_context(list(seed_by_ticker), decision_time)
            candidates = self._technical_candidates(contexts, seed_by_ticker, decision_time)
            top_count = max(5, min(8, int(self.profile.get("top_technical_candidates", 6))))
            selected = self._select_technical_candidates(candidates, top_count)
        except (AdapterError, RuntimeError, ValueError) as exc:
            return self._failed(cycle_id, "technical_discovery", exc, calls_before)

        researched: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        validated: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for candidate in selected:
            ticker = candidate["ticker"]
            try:
                instrument = self.discovery.validate_instrument(ticker)
            except Exception as exc:
                skipped.append({"ticker": ticker, "reason": f"instrument validation failed closed: {type(exc).__name__}"})
                continue
            if not instrument.get("valid", False):
                skipped.append({"ticker": ticker, "reason": instrument.get("reason")})
                continue
            validated.append((candidate, instrument))

        search_results: dict[str, tuple[list[dict[str, Any]], list[dict[str, Any]]] | Exception] = {}
        max_workers = max(1, min(4, int(self.profile.get("max_parallel_exa_searches", 3))))
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="ai-gated-exa") as pool:
            futures = {
                pool.submit(
                    self.news.search,
                    candidate["ticker"],
                    decision_time if now is not None else utc_now(),
                    company_name=instrument.get("name"),
                ): candidate["ticker"]
                for candidate, instrument in validated
            }
            for future in as_completed(futures):
                ticker = futures[future]
                try:
                    search_results[ticker] = future.result()
                except Exception as exc:
                    search_results[ticker] = exc

        for candidate, instrument in validated:
            ticker = candidate["ticker"]
            search_result = search_results.get(ticker)
            if isinstance(search_result, Exception) or search_result is None:
                error = search_result if isinstance(search_result, Exception) else RuntimeError("missing Exa result")
                skipped.append({"ticker": ticker, "reason": f"Exa failed closed: {type(error).__name__}: {error}"})
                continue
            events, sources = search_result
            events = self.evidence.normalize_events(events, ticker=ticker)
            snapshot_time = decision_time if now is not None else utc_now()
            immutable = self.evidence.write_snapshot(
                snapshot_type=f"ai-gated-{ticker}",
                decision_time=snapshot_time,
                payload={
                    "cycle_id": cycle_id,
                    "candidate": candidate,
                    "instrument": instrument,
                    "events": events,
                    "source_metadata": sources,
                },
            )
            eligible, model_events, reason = self.evidence.research_eligibility(
                f"ai_gated:{ticker}",
                events,
                snapshot_time,
                ticker_cooldown_minutes=int(self.profile.get("ticker_cooldown_minutes", 120)),
                event_cooldown_hours=int(self.profile.get("event_cooldown_hours", 24)),
            )
            if not eligible:
                skipped.append({"ticker": ticker, "reason": reason, "evidence_snapshot": immutable})
                continue
            researched.append(
                {
                    **candidate,
                    "events": model_events,
                    "source_metadata": sources,
                    "evidence_snapshot": immutable,
                    "instrument": instrument,
                    "research_time": snapshot_time,
                    "market_session": clock.market_session,
                }
            )

        if not researched:
            result = self._result(
                cycle_id,
                decision_time,
                candidates,
                selected,
                [],
                [],
                skipped,
                calls_before,
                research_only=research_only,
            )
            result["monitor"] = monitor
            append_jsonl(self.root, "ai_gated_cycles.jsonl", result)
            return result

        try:
            ranking = self.team.rank(
                snapshot_id=cycle_id,
                decision_time=max(item["research_time"] for item in researched),
                candidates=[self._ranking_payload(item) for item in researched],
            )
        except (ProviderError, ValueError) as exc:
            return self._failed(cycle_id, "model_ranking", exc, calls_before)

        ranked = self._validated_ranking(ranking, researched)
        deep_limit = int(self.profile.get("top_deep_research_candidates", 2))
        decisions: list[dict[str, Any]] = []
        orders: list[dict[str, Any]] = []
        for rank in ranked[:deep_limit]:
            item = next(value for value in researched if value["ticker"] == rank["ticker"])
            if hasattr(self.news, "search_primary_evidence"):
                try:
                    primary_events, primary_sources = self.news.search_primary_evidence(
                        item["ticker"],
                        item["research_time"],
                        company_name=item["instrument"].get("name"),
                    )
                    item["events"] = self.evidence.normalize_events(
                        [*item["events"], *primary_events],
                        ticker=item["ticker"],
                    )
                    item["source_metadata"] = [*item["source_metadata"], *primary_sources]
                    item["primary_evidence_snapshot"] = self.evidence.write_snapshot(
                        snapshot_type=f"ai-gated-primary-{item['ticker']}",
                        decision_time=item["research_time"],
                        payload={
                            "cycle_id": cycle_id,
                            "ticker": item["ticker"],
                            "events": primary_events,
                            "source_metadata": primary_sources,
                        },
                    )
                except AdapterError as exc:
                    item["primary_evidence_error"] = str(exc)
            snapshot = self._agent_snapshot(item, rank)
            analysis = self.team.analyze(snapshot, rank)
            execution = self._execute(
                analysis,
                snapshot,
                item,
                live_cycle=now is None,
                research_only=research_only,
            )
            analysis["execution"] = execution
            analysis["evidence_snapshot"] = item["evidence_snapshot"]
            append_jsonl(self.root, "ai_gated_decisions.jsonl", analysis)
            decisions.append(analysis)
            if execution.get("order"):
                orders.append(execution["order"])
            if not research_only:
                self._publish_signal(analysis, snapshot)
                self.evidence.mark_researched(
                    f"ai_gated:{item['ticker']}",
                    item["events"],
                    snapshot["decision_time"],
                )

        result = self._result(
            cycle_id,
            decision_time,
            candidates,
            selected,
            ranked,
            decisions,
            skipped,
            calls_before,
            orders=orders,
            research_only=research_only,
        )
        result["monitor"] = monitor
        append_jsonl(self.root, "ai_gated_cycles.jsonl", result)
        return result

    def monitor_only(self, now: str | None = None, *, force_flatten: bool = False) -> dict[str, Any]:
        decision_time = now or utc_now()
        clock = self.clock.status(decision_time)
        if not clock.is_regular:
            return {"event": "ai_gated_monitor_idle", "reason": f"market session is {clock.market_session}"}
        positions = self.broker.store.positions()
        option_positions = self.option_broker.store.positions()
        quotes: dict[str, Quote] = {}
        quote_errors: dict[str, str] = {}
        for symbol, position in positions.items():
            try:
                quotes[symbol] = self.discovery.fetch_current_quote(
                    symbol,
                    average_daily_volume_usd=position.average_price * 1_000_000,
                )
            except Exception as exc:
                quote_errors[symbol] = f"{type(exc).__name__}: {exc}"
        option_ids = set(option_positions)
        option_ids.update(
            order.contract.option_id
            for order in self.option_broker.store.orders().values()
            if order.status in {"created", "submitted_to_paper_broker", "open", "partially_filled"}
        )
        try:
            option_quotes = self.option_data.fetch_quotes(sorted(option_ids)) if option_ids else {}
        except Exception as exc:
            option_quotes = {}
            quote_errors["options"] = f"{type(exc).__name__}: {exc}"

        open_updates = [order.to_dict() for order in self.broker.process_open_orders(quotes, decision_time)]
        self.broker.lifecycle.mark_equity_quotes(quotes, decision_time)
        option_open_updates = [
            order.to_dict()
            for order in self.option_broker.process_open_orders(option_quotes, decision_time)
        ]
        self.option_broker.lifecycle.mark_option_quotes(option_quotes, decision_time)
        preclose = (
            clock.minutes_to_close is not None
            and clock.minutes_to_close <= int(self.config["paper"].get("exit_before_close_minutes", 10))
        )
        session_date = parse_ts(clock.open_time or decision_time).date()
        exits: list[dict[str, Any]] = []
        for symbol, position in list(self.broker.store.positions().items()):
            quote = quotes.get(symbol)
            decision = evaluate_position_exit(
                position,
                quote,
                decision_time,
                self.config["risk"],
                minutes_to_close=clock.minutes_to_close,
                exit_before_close_minutes=int(self.config["paper"].get("exit_before_close_minutes", 10)),
            )
            overnight = parse_ts(position.opened_at).date() < session_date
            mandatory_flatten = force_flatten or preclose or overnight
            should_exit = mandatory_flatten or decision.should_exit
            reason = (
                "AI sleeve mandatory flatten"
                if force_flatten or preclose
                else ("AI sleeve overnight recovery flatten" if overnight else decision.reason)
            )
            if not should_exit or quote is None:
                continue
            execution_now = max(parse_ts(decision_time), parse_ts(quote.asof)).isoformat()
            order = self.broker.create_order(
                decision_id=f"ai_monitor:{clock.session}:{symbol}:{reason}",
                symbol=symbol,
                side="sell",
                order_type="market",
                quantity=position.quantity,
                limit_price=None,
                quote_seen_at=quote.asof,
                thesis=reason,
                idempotency_key=f"ai_monitor:{clock.session}:{symbol}:{reason}",
                now=execution_now,
            )
            submitted = self.broker.submit_order(order, quote, execution_now)
            write_order_journal(
                self.root,
                submitted,
                note=reason,
                namespace=self.namespace,
            )
            exits.append({"instrument": "equity", "reason": reason, "order": submitted.to_dict()})

        option_exits: list[dict[str, Any]] = []
        for option_id, position in list(self.option_broker.store.positions().items()):
            quote = option_quotes.get(option_id)
            decision = evaluate_option_exit(position, quote, decision_time, self.config["options_risk"])
            overnight = parse_ts(position.opened_at).date() < session_date
            mandatory_flatten = force_flatten or preclose or overnight
            should_exit = mandatory_flatten or decision.should_exit
            reason = (
                "AI sleeve mandatory flatten"
                if force_flatten or preclose
                else ("AI sleeve overnight recovery flatten" if overnight else decision.reason)
            )
            if not should_exit or quote is None:
                continue
            execution_now = max(parse_ts(decision_time), parse_ts(quote.updated_at)).isoformat()
            order = self.option_broker.create_order(
                decision_id=f"ai_monitor:{clock.session}:{option_id}:{reason}",
                contract=position.contract,
                intent="sell_to_close",
                order_type="market",
                quantity=position.quantity,
                limit_price=None,
                quote_seen_at=quote.updated_at,
                thesis=reason,
                idempotency_key=f"ai_monitor:{clock.session}:{option_id}:{reason}",
                now=execution_now,
            )
            submitted = self.option_broker.submit_order(order, quote, execution_now)
            option_exits.append({"instrument": position.contract.option_type, "reason": reason, "order": submitted.to_dict()})

        account = self.broker.store.account()
        current_positions = self.broker.store.positions()
        current_option_positions = self.option_broker.store.positions()
        missing_equity = sorted(symbol for symbol in current_positions if symbol not in quotes)
        missing_options = sorted(option_id for option_id in current_option_positions if option_id not in option_quotes)
        equity = None
        if not missing_equity and not missing_options:
            equity = account.equity(current_positions, quotes)
            equity += sum(
                position.liquidation_value(option_quotes.get(option_id))
                for option_id, position in current_option_positions.items()
            )
        portfolio = {
            "event": "ai_gated_portfolio_snapshot",
            "session": clock.session,
            "asof": decision_time,
            "cash": round(account.cash, 4),
            "equity": round(equity, 4) if equity is not None else None,
            "realized_pnl": round(account.realized_pnl, 4),
            "positions": {key: value.to_dict() for key, value in current_positions.items()},
            "option_positions": {key: value.to_dict() for key, value in current_option_positions.items()},
            "missing_position_quotes": missing_equity,
            "missing_option_quotes": missing_options,
        }
        append_jsonl(
            self.root,
            f"strategy_sleeves/{self.namespace}/portfolio_snapshots.jsonl",
            portfolio,
        )
        return {
            "event": "ai_gated_monitor_complete",
            "open_order_updates": open_updates,
            "option_open_order_updates": option_open_updates,
            "exits": exits,
            "option_exits": option_exits,
            "quote_errors": quote_errors,
            "portfolio": portfolio,
            "live_order_tools_called": False,
        }

    def _technical_candidates(
        self,
        contexts: dict[str, dict[str, Any]],
        seeds: dict[str, dict[str, Any]],
        decision_time: str,
    ) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        minimum = float(self.profile.get("minimum_technical_prefilter_score", 0.48))
        market_session = self.clock.status(decision_time)
        for ticker, context in contexts.items():
            if not context.get("eligible", False):
                continue
            signals = dict(context.get("technical_signals", {}))
            move_1d = float(signals.get("price_change_1d_pct") or 0)
            move_5d = float(signals.get("price_change_5d_pct") or 0)
            signals["chase_score"] = max(0.0, min(1.0, max(move_1d / 8.0, move_5d / 15.0)))
            snapshot = {
                "technical_signals": signals,
                "market_data": {"market_regime": "neutral"},
            }
            bullish = weighted_score(directional_feature_scores(snapshot, "bullish"), DEFAULT_WEIGHTS)
            bearish = weighted_score(directional_feature_scores(snapshot, "bearish"), DEFAULT_WEIGHTS)
            score = max(bullish, bearish)
            seed = seeds.get(ticker, {})
            reported_earnings = self._confirmed_reported_earnings(
                seed,
                market_session.session,
            )
            if score < minimum and reported_earnings is None:
                continue
            candidates.append(
                {
                    "ticker": ticker,
                    "eligible": True,
                    "pre_score": score,
                    "technical_direction": "bullish" if bullish >= bearish else "bearish",
                    "bullish_score": bullish,
                    "bearish_score": bearish,
                    "market_context": {**context, "technical_signals": signals},
                    "seed_sources": seed.get("sources", []),
                    "reported_earnings": reported_earnings,
                    "decision_time": decision_time,
                }
            )
        candidates.sort(key=lambda item: float(item["pre_score"]), reverse=True)
        return candidates

    def _select_technical_candidates(
        self,
        candidates: list[dict[str, Any]],
        top_count: int,
    ) -> list[dict[str, Any]]:
        selected: list[dict[str, Any]] = []
        seen: set[str] = set()

        def add(item: dict[str, Any], reason: str) -> None:
            ticker = str(item["ticker"])
            if ticker in seen or len(selected) >= top_count:
                return
            selected.append({**item, "selection_reason": reason})
            seen.add(ticker)

        priority_limit = max(
            0,
            int(self.profile.get("reported_earnings_priority_candidates", 2)),
        )
        reported = sorted(
            (
                item
                for item in candidates
                if isinstance(item.get("reported_earnings"), dict)
            ),
            key=lambda item: abs(
                float(
                    item["reported_earnings"].get("eps_surprise_ratio")
                    or 0
                )
            ),
            reverse=True,
        )
        for item in reported[:priority_limit]:
            add(item, "confirmed_reported_earnings")

        minimum_per_direction = max(
            0,
            int(self.profile.get("minimum_candidates_per_direction", 2)),
        )
        for direction in ("bullish", "bearish"):
            current = sum(
                item["technical_direction"] == direction
                for item in selected
            )
            for item in candidates:
                if current >= minimum_per_direction:
                    break
                if item["technical_direction"] != direction:
                    continue
                before = len(selected)
                add(item, f"top_{direction}_technical")
                if len(selected) > before:
                    current += 1

        for item in candidates:
            add(item, "top_overall_technical")
        return selected

    @staticmethod
    def _confirmed_reported_earnings(
        seed: dict[str, Any],
        market_session_date: str,
    ) -> dict[str, Any] | None:
        confirmed: list[dict[str, Any]] = []
        for source in seed.get("source_details", []):
            if source.get("source") != "earnings_calendar":
                continue
            detail = source.get("detail", {})
            eps = detail.get("eps", {}) if isinstance(detail.get("eps"), dict) else {}
            report = (
                detail.get("report", {})
                if isinstance(detail.get("report"), dict)
                else {}
            )
            actual = eps.get("actual")
            report_date = str(report.get("date") or "")
            timing = str(report.get("timing") or "").lower()
            if actual is None or not report_date or report_date > market_session_date:
                continue
            if report_date == market_session_date and timing not in {"am", "before_market"}:
                continue
            confirmed.append(
                {
                    "report_date": report_date,
                    "timing": timing or None,
                    "verified": bool(report.get("verified", False)),
                    "eps_actual": float(actual),
                    "eps_estimate": (
                        float(eps["estimate"])
                        if eps.get("estimate") is not None
                        else None
                    ),
                    "eps_surprise_ratio": detail.get("eps_surprise_ratio"),
                    "source": "robinhood_mcp:get_earnings_calendar",
                }
            )
        if not confirmed:
            return None
        confirmed.sort(key=lambda item: item["report_date"], reverse=True)
        return confirmed[0]

    @staticmethod
    def _ranking_payload(item: dict[str, Any]) -> dict[str, Any]:
        compact_events = [
            {
                key: event.get(key)
                for key in (
                    "headline",
                    "published_at",
                    "event_at",
                    "source",
                    "source_tier",
                    "url",
                    "highlights",
                )
                if event.get(key) is not None
            }
            for event in item["events"][:6]
        ]
        return {
            "ticker": item["ticker"],
            "eligible": True,
            "pre_score": item["pre_score"],
            "technical_direction": item["technical_direction"],
            "selection_reason": item.get("selection_reason"),
            "seed_sources": item.get("seed_sources", []),
            "reported_earnings": item.get("reported_earnings"),
            "market_context": {
                "quote": item["market_context"]["quote"],
                "technical_signals": item["market_context"]["technical_signals"],
                "fundamentals": item["market_context"].get("fundamentals", {}),
            },
            "events": compact_events,
        }

    def _validated_ranking(
        self,
        ranking: dict[str, Any],
        researched: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        allowed = {item["ticker"] for item in researched}
        minimum = float(self.profile.get("minimum_model_rank_score", 0.55))
        seen: set[str] = set()
        result: list[dict[str, Any]] = []
        for item in ranking.get("ranked_candidates", []):
            ticker = str(item.get("ticker", "")).upper()
            score = float(item.get("score", 0))
            if ticker not in allowed or ticker in seen or score < minimum:
                continue
            seen.add(ticker)
            result.append({**item, "ticker": ticker})
        result.sort(key=lambda item: float(item["score"]), reverse=True)
        return result

    def _agent_snapshot(self, item: dict[str, Any], ranking: dict[str, Any]) -> dict[str, Any]:
        cutoff_candidates = [item["research_time"]]
        cutoff_candidates.extend(
            str(event.get("retrieved_at") or event.get("first_seen_at"))
            for event in item["events"]
            if event.get("retrieved_at") or event.get("first_seen_at")
        )
        decision_time = max(parse_ts(value) for value in cutoff_candidates).isoformat()
        context = item["market_context"]
        return {
            "snapshot_id": f"aigt_{item['ticker']}_{uuid4().hex}",
            "decision_time": decision_time,
            "data_cutoff_time": decision_time,
            "ticker": item["ticker"],
            "market_session": item.get("market_session", "regular"),
            "market_data": {
                "quote": context["quote"],
                "fundamentals": context.get("fundamentals", {}),
                "reported_earnings": item.get("reported_earnings"),
                "market_regime": "neutral",
                "binary_event_within_days": 99,
                "has_position": item["ticker"] in self.broker.store.positions(),
                "paper_sleeve": self.namespace,
                "paper_account": self.broker.store.account().to_dict(),
            },
            "technical_signals": context.get("technical_signals", {}),
            "available_news": item["events"],
            "source_metadata": [
                {"source": "robinhood_mcp", "source_tier": 1, "retrieved_at": decision_time},
                *item["source_metadata"],
            ],
            "agent_context": {"ranking": ranking},
        }

    def _execute(
        self,
        analysis: dict[str, Any],
        snapshot: dict[str, Any],
        item: dict[str, Any],
        *,
        live_cycle: bool,
        research_only: bool = False,
    ) -> dict[str, Any]:
        decision = analysis["decision"]
        minimum_confidence = float(self.profile.get("minimum_decision_confidence", 0.58))
        if analysis.get("fail_closed") or decision["action"] == "no_trade":
            return {"status": "no_trade", "reason": decision.get("no_trade_reason"), "order": None}
        if float(decision.get("confidence", 0)) < minimum_confidence:
            return {
                "status": "no_trade",
                "reason": f"model confidence below deterministic floor {minimum_confidence:g}",
                "order": None,
            }
        if research_only:
            return {
                "status": "research_only",
                "reason": (
                    "premarket plan requires a fresh regular-session quote "
                    "and deterministic risk revalidation"
                ),
                "order": None,
                "live_order_tools_called": False,
            }
        entry_line = "equity" if decision["instrument"] == "equity" else "options"
        block_reason = self._entry_block_reason(entry_line, snapshot["decision_time"])
        if block_reason:
            return {"status": "no_trade", "reason": block_reason, "order": None}
        ticker = snapshot["ticker"]
        observed = Quote(**item["market_context"]["quote"])
        try:
            quote = self.discovery.fetch_current_quote(
                ticker,
                average_daily_volume_usd=observed.avg_daily_volume_usd,
                asset_class=observed.asset_class,
            )
        except Exception as exc:
            return {"status": "no_trade", "reason": f"quote refresh failed closed: {type(exc).__name__}", "order": None}
        observed_now = utc_now() if live_cycle else snapshot["decision_time"]
        now = max(
            parse_ts(snapshot["decision_time"]),
            parse_ts(quote.asof),
            parse_ts(observed_now),
        ).isoformat()
        if decision["instrument"] == "equity":
            if not self.profile.get("allow_equity", True):
                return {"status": "no_trade", "reason": "equity disabled for AI sleeve", "order": None}
            quantity = calculate_entry_quantity(
                self.broker.store.account(),
                self.broker.store.positions(),
                quote,
                self.config["risk"],
                notional_buffer_pct=float(
                    self.config.get("integrations", {}).get("runtime", {}).get("order_notional_buffer_pct", 0.96)
                ),
            )
            if quantity <= 0:
                return {"status": "no_trade", "reason": "AI sleeve has no risk capacity", "order": None}
            order = self.broker.create_order(
                decision_id=snapshot["snapshot_id"],
                symbol=ticker,
                side="buy",
                order_type="limit",
                quantity=quantity,
                limit_price=self._equity_buy_limit(quote.ask),
                quote_seen_at=quote.asof,
                thesis=decision["thesis"],
                idempotency_key=f"{self.STRATEGY}:{snapshot['snapshot_id']}:equity",
                now=now,
            )
            submitted = self.broker.submit_order(order, quote, now)
        else:
            option_type = str(decision["instrument"])
            if option_type == "call" and not self.profile.get("allow_long_call", True):
                return {"status": "no_trade", "reason": "long calls disabled for AI sleeve", "order": None}
            if option_type == "put" and not self.profile.get("allow_long_put", True):
                return {"status": "no_trade", "reason": "long puts disabled for AI sleeve", "order": None}
            account = self.option_broker.store.base.account()
            equity_positions = self.option_broker.store.base.positions()
            option_positions = self.option_broker.store.positions()
            equity_at_cost = account.cash
            equity_at_cost += sum(position.average_price * position.quantity for position in equity_positions.values())
            equity_at_cost += sum(position.cost_basis() for position in option_positions.values())
            budget = equity_at_cost * float(self.config["options_risk"].get("max_order_risk_pct_of_equity", 0.10)) * 0.95
            try:
                selected = self.option_data.fetch_best_contract(
                    underlying=ticker,
                    underlying_price=quote.last,
                    option_type=option_type,
                    now=now,
                    max_premium_usd=budget,
                )
            except Exception as exc:
                return {
                    "status": "no_trade",
                    "reason": f"option selection failed closed: {type(exc).__name__}: {exc}",
                    "order": None,
                }
            if selected is None:
                return {"status": "no_trade", "reason": "no option passed deterministic liquidity/risk filters", "order": None}
            contract, option_quote = selected
            order = self.option_broker.create_order(
                decision_id=snapshot["snapshot_id"],
                contract=contract,
                intent="buy_to_open",
                order_type="limit",
                quantity=1,
                limit_price=self._option_buy_limit(option_quote.ask, contract.below_tick or 0.01),
                quote_seen_at=option_quote.updated_at,
                thesis=decision["thesis"],
                idempotency_key=f"{self.STRATEGY}:{snapshot['snapshot_id']}:{contract.option_id}",
                now=now,
            )
            submitted = self.option_broker.submit_order(order, option_quote, now)
        return {
            "status": submitted.status,
            "reason": submitted.reject_reason,
            "paper_sleeve": self.namespace,
            "order": submitted.to_dict(),
            "live_order_tools_called": False,
        }

    def _entry_block_reason(self, line: str, now: str) -> str | None:
        session_date = parse_ts(self.clock.status(now).open_time or now).date()
        positions = [
            *self.broker.store.positions().values(),
            *self.option_broker.store.positions().values(),
        ]
        if any(parse_ts(position.opened_at).date() < session_date for position in positions):
            return "overnight recovery in progress"
        return daily_entry_limit_reason(
            line,
            self.broker.store.daily_counters(now),
            self.config,
        )

    def _equity_buy_limit(self, ask: float) -> float:
        costs = self.config.get("costs", {})
        slip = max(
            ask * float(costs.get("slippage_bps", 0)) / 10000,
            float(costs.get("minimum_slippage_usd", 0)),
        )
        buffer_bps = float(
            self.config.get("integrations", {}).get("runtime", {}).get("aggressive_limit_buffer_bps", 0)
        )
        return round(ask + max(slip, ask * buffer_bps / 10000), 4)

    def _option_buy_limit(self, ask: float, tick: float) -> float:
        costs = self.config.get("options_costs", {})
        slip = max(
            ask * float(costs.get("slippage_bps", 0)) / 10000,
            float(costs.get("minimum_slippage_usd_per_contract", 0)),
        )
        return round(math.ceil((ask + slip) / tick) * tick, 4)

    def _publish_signal(self, analysis: dict[str, Any], snapshot: dict[str, Any]) -> None:
        bull = analysis.get("bull_news")
        if not isinstance(bull, dict):
            return
        source_tiers = [
            int(event.get("source_tier", 4))
            for event in snapshot.get("available_news", [])
            if event.get("source_tier") is not None
        ]
        self.signals.put(
            snapshot["ticker"],
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

    def _result(
        self,
        cycle_id: str,
        decision_time: str,
        candidates: list[dict[str, Any]],
        selected: list[dict[str, Any]],
        ranked: list[dict[str, Any]],
        decisions: list[dict[str, Any]],
        skipped: list[dict[str, Any]],
        calls_before: int,
        *,
        orders: list[dict[str, Any]] | None = None,
        research_only: bool = False,
    ) -> dict[str, Any]:
        account = self.broker.store.account()
        return {
            "event": "ai_gated_cycle_complete",
            "strategy": self.STRATEGY,
            "execution": "isolated_paper_sleeve",
            "cycle_id": cycle_id,
            "decision_time": decision_time,
            "technical_candidate_count": len(candidates),
            "technical_top_set": [item["ticker"] for item in selected],
            "ranked_candidates": ranked,
            "decisions": decisions,
            "skipped": skipped,
            "paper_orders": orders or [],
            "paper_orders_created": len(orders or []),
            "model_calls": len(self.tracker.records) - calls_before,
            "usage": self.tracker.summary(),
            "paper_sleeve": self.namespace,
            "paper_account": account.to_dict(),
            "research_only": research_only,
            "live_order_tools_called": False,
        }

    def _premarket_research_allowed(
        self,
        clock: Any,
        decision_time: str,
    ) -> bool:
        if (
            clock.market_session != "pre_market"
            or not self.profile.get("allow_premarket_research", False)
            or not clock.open_time
        ):
            return False
        minutes_to_open = (
            parse_ts(clock.open_time) - parse_ts(decision_time)
        ).total_seconds() / 60
        window = float(
            self.profile.get("premarket_research_window_minutes", 90)
        )
        return 0 < minutes_to_open <= window

    def _failed(
        self,
        cycle_id: str,
        stage: str,
        exc: Exception,
        calls_before: int,
    ) -> dict[str, Any]:
        result = {
            "event": "ai_gated_cycle_failed_closed",
            "strategy": self.STRATEGY,
            "cycle_id": cycle_id,
            "stage": stage,
            "reason": f"{type(exc).__name__}: {exc}",
            "model_calls": len(self.tracker.records) - calls_before,
            "paper_orders_created": 0,
            "live_order_tools_called": False,
        }
        append_jsonl(self.root, "ai_gated_cycles.jsonl", result)
        return result
