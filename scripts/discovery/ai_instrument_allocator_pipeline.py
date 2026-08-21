from __future__ import annotations

from datetime import timedelta
import math
from pathlib import Path
from typing import Any
from uuid import uuid4

from scripts.agents.ai_instrument_allocator_team import AiInstrumentAllocatorTeam
from scripts.core.audit import append_jsonl
from scripts.core.models import Quote, parse_ts, utc_now
from scripts.decision.instrument_allocator import allocate_instrument
from scripts.decision.signed_return_signal import (
    derive_signal_summary,
    validate_actionable_signal,
)
from scripts.discovery.ai_gated_pipeline import AiGatedPaperPipeline
from scripts.discovery.evidence_store import EvidenceSnapshotStore
from scripts.exit.evaluate_exit import evaluate_position_exit
from scripts.exit.position_mandates import (
    PositionMandateStore,
    evaluate_mandate_exit,
    planned_exit_time,
    validate_position_mandate,
)
from scripts.evaluation.probability_calibration import uncalibrated_metadata
from scripts.journal.write_trade_journal import write_order_journal
from scripts.llm.base_provider import LLMProvider, ProviderError
from scripts.llm.usage_tracker import UsageTracker
from scripts.options.exit_policy import evaluate_option_exit
from scripts.options.risk_gate import validate_option_quote
from scripts.risk.risk_gate import validate_quote
from scripts.risk.shared_portfolio_risk import shared_deployment
from scripts.strategies.allocator_state import AllocatorStateStore


class AiInstrumentAllocatorPipeline(AiGatedPaperPipeline):
    """Conditional research plans plus a deterministic equity/option allocator."""

    STRATEGY = "ai_instrument_allocator_v1"
    RESEARCH_STAGES = {
        "overnight",
        "premarket_update",
        "preopen_revalidation",
        "intraday",
    }
    EXECUTION_STAGES = {"open_execution", "intraday"}

    def __init__(
        self,
        root: str | Path,
        config: dict[str, Any],
        provider: LLMProvider,
        tracker: UsageTracker,
        **kwargs: Any,
    ) -> None:
        super().__init__(root, config, provider, tracker, **kwargs)
        self.team = AiInstrumentAllocatorTeam(config, provider, tracker)
        self.evidence = EvidenceSnapshotStore(
            self.root,
            namespace="ai_instrument_allocator",
        )
        self.plans = AllocatorStateStore(self.root, namespace=self.namespace)
        self.mandates = PositionMandateStore(self.root, namespace=self.namespace)

    def run_stage(self, stage: str, now: str | None = None) -> dict[str, Any]:
        decision_time = now or utc_now()
        if stage not in self.RESEARCH_STAGES | {"open_execution"}:
            raise ValueError(f"unsupported allocator stage: {stage}")
        if not self.profile.get("enabled", False):
            return self._stage_result(
                stage,
                decision_time,
                reason="strategy disabled",
            )
        monitor = self.monitor_only(decision_time)
        if not self.profile.get("new_entries_enabled", True):
            return self._stage_result(
                stage,
                decision_time,
                reason="new allocator entries disabled",
                monitor=monitor,
            )
        if not self._stage_session_allowed(stage, decision_time):
            return self._stage_result(
                stage,
                decision_time,
                reason=f"stage is outside its market window: {self.clock.status(decision_time).market_session}",
                monitor=monitor,
            )

        calls_before = len(self.tracker.records)
        if stage == "open_execution":
            executions = [
                self._execute_plan(plan, decision_time, stage=stage)
                for plan in self.plans.active_plans(decision_time)
            ]
            return self._stage_result(
                stage,
                decision_time,
                executions=executions,
                model_calls=len(self.tracker.records) - calls_before,
                monitor=monitor,
            )

        if stage == "premarket_update":
            research = self._premarket_update(decision_time)
        elif stage == "preopen_revalidation":
            research = self._preopen_revalidation(decision_time)
        else:
            research = self._research_stage(stage, decision_time)
        return self._stage_result(
            stage,
            decision_time,
            plans=research["plans"],
            executions=research["executions"],
            skipped=research["skipped"],
            model_calls=len(self.tracker.records) - calls_before,
            monitor=monitor,
        )

    def monitor_only(
        self,
        now: str | None = None,
        *,
        force_flatten: bool = False,
    ) -> dict[str, Any]:
        decision_time = now or utc_now()
        clock = self.clock.status(decision_time)
        recovery_updates = self._recover_allocator_entry_orders(decision_time)
        self.mandates.reconcile(
            equity_orders=self.broker.store.orders(),
            option_orders=self.option_broker.store.orders(),
            now=decision_time,
        )
        if not clock.is_regular:
            return {
                "event": "ai_instrument_allocator_monitor_idle",
                "reason": f"market session is {clock.market_session}",
                "recovery_order_updates": recovery_updates,
                "live_order_tools_called": False,
            }

        positions = self.broker.store.positions()
        option_positions = self.option_broker.store.positions()
        quotes: dict[str, Quote] = {}
        option_quotes: dict[str, Any] = {}
        errors: dict[str, str] = {}
        equity_symbols = set(positions)
        equity_symbols.update(
            order.symbol
            for order in self.broker.store.orders().values()
            if order.status
            in {"submitted_to_paper_broker", "open", "partially_filled"}
        )
        for symbol in equity_symbols:
            position = positions.get(symbol)
            try:
                quotes[symbol] = self.discovery.fetch_current_quote(
                    symbol,
                    average_daily_volume_usd=(
                        position.average_price * 1_000_000
                        if position is not None
                        else None
                    ),
                )
            except Exception as exc:
                errors[symbol] = f"{type(exc).__name__}: {exc}"
        option_ids = set(option_positions)
        option_ids.update(
            order.contract.option_id
            for order in self.option_broker.store.orders().values()
            if order.status in {"created", "submitted_to_paper_broker", "open", "partially_filled"}
        )
        if option_ids:
            try:
                option_quotes = self.option_data.fetch_quotes(sorted(option_ids))
            except Exception as exc:
                errors["options"] = f"{type(exc).__name__}: {exc}"

        entry_nav_usd: float | None = None
        try:
            entry_nav_usd = float(
                self._account_state(
                    decision_time,
                    equity_quotes=quotes,
                    option_quotes=option_quotes,
                )["nav_usd"]
            )
        except (TypeError, ValueError) as exc:
            errors["marked_nav"] = f"{type(exc).__name__}: {exc}"
            recovery_updates.extend(
                self._recover_allocator_entry_orders(
                    decision_time,
                    cancel_retryable_reason=(
                        "allocator marked NAV unavailable during retry"
                    ),
                )
            )

        open_updates = [
            order.to_dict()
            for order in self.broker.process_open_orders(
                quotes,
                decision_time,
                entry_nav_usd=entry_nav_usd,
            )
        ]
        option_open_updates = [
            order.to_dict()
            for order in self.option_broker.process_open_orders(
                option_quotes,
                decision_time,
                entry_nav_usd=entry_nav_usd,
            )
        ]
        self.mandates.reconcile(
            equity_orders=self.broker.store.orders(),
            option_orders=self.option_broker.store.orders(),
            now=decision_time,
        )

        exits: list[dict[str, Any]] = []
        for symbol, position in list(self.broker.store.positions().items()):
            quote = quotes.get(symbol)
            exposure_id = f"equity:{symbol}"
            mandate = self.mandates.for_exposure(exposure_id)
            mandate_exit = evaluate_mandate_exit(
                mandate,
                decision_time,
                expected_exposure_id=exposure_id,
                expected_ticker=symbol,
                expected_instrument_type="equity",
            )
            price_exit = evaluate_position_exit(
                position,
                quote,
                decision_time,
                self.config["risk"],
                minutes_to_close=None,
                exit_before_close_minutes=int(
                    self.config["paper"].get("exit_before_close_minutes", 10)
                ),
                apply_legacy_time_stop=False,
                stop_price_override=(
                    float(mandate["planned_stop_price"])
                    if mandate is not None and not mandate_exit.should_exit
                    else None
                ),
            )
            reason = (
                "allocator force flatten"
                if force_flatten
                else mandate_exit.reason
                if mandate_exit.should_exit
                else price_exit.reason
            )
            if not (force_flatten or mandate_exit.should_exit or price_exit.should_exit) or quote is None:
                continue
            execution_now = max(parse_ts(decision_time), parse_ts(quote.asof)).isoformat()
            order = self.broker.create_order(
                decision_id=f"allocator_exit:{clock.session}:{symbol}:{reason}",
                symbol=symbol,
                side="sell",
                order_type="market",
                quantity=position.quantity,
                limit_price=None,
                quote_seen_at=quote.asof,
                thesis=reason,
                strategy=self.STRATEGY,
                signal_horizon=mandate.get("horizon") if mandate else None,
                idempotency_key=f"allocator_exit:{clock.session}:{symbol}:{reason}",
                now=execution_now,
            )
            submitted = self.broker.submit_order(order, quote, execution_now)
            write_order_journal(
                self.root,
                submitted,
                note=reason,
                namespace=self.namespace,
            )
            if submitted.status == "filled":
                self.mandates.close(
                    f"equity:{symbol}",
                    reason=reason,
                    now=execution_now,
                )
            exits.append({"instrument": "equity", "reason": reason, "order": submitted.to_dict()})

        option_exits: list[dict[str, Any]] = []
        for option_id, position in list(self.option_broker.store.positions().items()):
            quote = option_quotes.get(option_id)
            exposure_id = f"option:{option_id}"
            mandate = self.mandates.for_exposure(exposure_id)
            mandate_exit = evaluate_mandate_exit(
                mandate,
                decision_time,
                expected_exposure_id=exposure_id,
                expected_ticker=position.contract.underlying,
                expected_instrument_type=position.contract.option_type,
            )
            price_exit = evaluate_option_exit(
                position,
                quote,
                decision_time,
                self.config["options_risk"],
                apply_legacy_time_stop=False,
            )
            reason = (
                "allocator force flatten"
                if force_flatten
                else mandate_exit.reason
                if mandate_exit.should_exit
                else price_exit.reason
            )
            if not (force_flatten or mandate_exit.should_exit or price_exit.should_exit) or quote is None:
                continue
            execution_now = max(
                parse_ts(decision_time),
                parse_ts(quote.updated_at),
            ).isoformat()
            order = self.option_broker.create_order(
                decision_id=f"allocator_exit:{clock.session}:{option_id}:{reason}",
                contract=position.contract,
                intent="sell_to_close",
                order_type="market",
                quantity=position.quantity,
                limit_price=None,
                quote_seen_at=quote.updated_at,
                thesis=reason,
                strategy=self.STRATEGY,
                signal_horizon=mandate.get("horizon") if mandate else None,
                idempotency_key=f"allocator_exit:{clock.session}:{option_id}:{reason}",
                now=execution_now,
            )
            submitted = self.option_broker.submit_order(order, quote, execution_now)
            if submitted.status == "filled":
                self.mandates.close(
                    f"option:{option_id}",
                    reason=reason,
                    now=execution_now,
                )
            option_exits.append(
                {
                    "instrument": position.contract.option_type,
                    "reason": reason,
                    "order": submitted.to_dict(),
                }
            )

        account = self.broker.store.account()
        deployment = shared_deployment(
            account,
            self.broker.store.positions(),
            self.option_broker.store.positions(),
            self.broker.store.orders(),
            self.option_broker.store.orders(),
        )
        try:
            marked_account = self._account_state(
                decision_time,
                equity_quotes=quotes,
                option_quotes=option_quotes,
            )
        except (TypeError, ValueError) as exc:
            errors["marked_nav"] = f"{type(exc).__name__}: {exc}"
            marked_account = None
        portfolio = {
            "event": "ai_instrument_allocator_portfolio_snapshot",
            "asof": decision_time,
            "cash": round(account.cash, 6),
            "initial_cash": account.initial_cash,
            **deployment,
            "marked_nav_usd": (
                marked_account["nav_usd"] if marked_account is not None else None
            ),
            "nav_valuation_method": (
                marked_account["nav_valuation_method"]
                if marked_account is not None
                else None
            ),
            "nav_calculated_at": (
                marked_account["nav_calculated_at"]
                if marked_account is not None
                else None
            ),
            "equity_mark_times": (
                marked_account["equity_mark_times"]
                if marked_account is not None
                else {}
            ),
            "option_mark_times": (
                marked_account["option_mark_times"]
                if marked_account is not None
                else {}
            ),
            "positions": {
                key: value.to_dict()
                for key, value in self.broker.store.positions().items()
            },
            "option_positions": {
                key: value.to_dict()
                for key, value in self.option_broker.store.positions().items()
            },
        }
        append_jsonl(
            self.root,
            f"strategy_sleeves/{self.namespace}/portfolio_snapshots.jsonl",
            portfolio,
        )
        return {
            "event": "ai_instrument_allocator_monitor_complete",
            "open_order_updates": open_updates,
            "option_open_order_updates": option_open_updates,
            "exits": exits,
            "option_exits": option_exits,
            "recovery_order_updates": recovery_updates,
            "quote_errors": errors,
            "portfolio": portfolio,
            "live_order_tools_called": False,
        }

    def _premarket_update(self, decision_time: str) -> dict[str, Any]:
        plans: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for prior_plan in self.plans.active_plans(decision_time):
            try:
                refreshed = self._refresh_plan_evidence(
                    prior_plan,
                    decision_time,
                    stage="premarket_update",
                )
            except Exception as exc:
                reason = f"incremental evidence refresh failed closed: {type(exc).__name__}: {exc}"
                self.plans.set_plan_status(
                    str(prior_plan["plan_id"]),
                    "invalidated",
                    reason=reason,
                    now=decision_time,
                )
                skipped.append(
                    {
                        "ticker": prior_plan["ticker"],
                        "reason": reason,
                    }
                )
                continue
            if refreshed is None:
                skipped.append(
                    {
                        "ticker": prior_plan["ticker"],
                        "reason": "no new evidence; active plan retained",
                    }
                )
                continue
            snapshot, new_events, evidence_snapshot = refreshed
            ranking = dict(prior_plan.get("ranking", {}))
            try:
                analysis = self.team.analyze(
                    snapshot,
                    ranking,
                    stage="premarket_update",
                    prior_signal=dict(prior_plan["signal"]),
                    new_events=new_events,
                )
            except Exception as exc:
                reason = f"premarket model revalidation failed closed: {type(exc).__name__}: {exc}"
                self.plans.set_plan_status(
                    str(prior_plan["plan_id"]),
                    "invalidated",
                    reason=reason,
                    now=decision_time,
                )
                skipped.append({"ticker": prior_plan["ticker"], "reason": reason})
                continue
            signal = analysis["signal"]
            record = {
                **analysis,
                "decision_time": snapshot["decision_time"],
                "data_cutoff_time": snapshot["data_cutoff_time"],
                "evidence_snapshot": evidence_snapshot,
                "prior_plan_id": prior_plan["plan_id"],
                "incremental_update": True,
                "incremental_event_count": len(new_events),
                **uncalibrated_metadata(str(signal["horizon"])),
            }
            actionable = not analysis.get("fail_closed") and signal.get(
                "action"
            ) == "propose_trade"
            if not actionable:
                reason = signal.get("no_trade_reason") or "premarket update failed closed"
                self.plans.set_plan_status(
                    str(prior_plan["plan_id"]),
                    "invalidated",
                    reason=reason,
                    now=decision_time,
                )
                skipped.append({"ticker": prior_plan["ticker"], "reason": reason})
            else:
                plan = self.plans.replace_plan(
                    str(prior_plan["plan_id"]),
                    {
                        "plan_id": f"plan_{uuid4().hex}",
                        "strategy": self.STRATEGY,
                        "ticker": prior_plan["ticker"],
                        "created_at": snapshot["decision_time"],
                        "valid_until": signal["thesis_valid_until"],
                        "status": "active",
                        "stage": "premarket_update",
                        "signal": signal,
                        "snapshot": snapshot,
                        "ranking": ranking,
                        "evidence_snapshot": evidence_snapshot,
                    },
                    reason="replaced by incremental premarket analysis",
                    now=decision_time,
                )
                plans.append(plan)
            append_jsonl(
                self.root,
                f"strategy_sleeves/{self.namespace}/decisions.jsonl",
                record,
            )
            self.evidence.mark_researched(
                f"allocator:{prior_plan['ticker']}",
                new_events,
                decision_time,
            )
        return {"plans": plans, "executions": [], "skipped": skipped}

    def _preopen_revalidation(self, decision_time: str) -> dict[str, Any]:
        plans: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for plan in self.plans.active_plans(decision_time):
            try:
                refreshed = self._refresh_plan_evidence(
                    plan,
                    decision_time,
                    stage="preopen_revalidation",
                )
            except Exception as exc:
                reason = f"pre-open evidence refresh failed closed: {type(exc).__name__}: {exc}"
                self.plans.set_plan_status(
                    str(plan["plan_id"]),
                    "invalidated",
                    reason=reason,
                    now=decision_time,
                )
                skipped.append({"ticker": plan["ticker"], "reason": reason})
                continue
            if refreshed is None:
                updated = self.plans.save_plan(
                    {
                        **plan,
                        "stage": "preopen_revalidation",
                        "preopen_revalidated_at": decision_time,
                        "updated_at": decision_time,
                    }
                )
                plans.append(updated)
                skipped.append(
                    {
                        "ticker": plan["ticker"],
                        "reason": "no new evidence; active plan retained",
                    }
                )
                continue
            snapshot, new_events, evidence_snapshot = refreshed
            try:
                assessment = self.team.revalidate(
                    snapshot,
                    dict(plan.get("ranking", {})),
                    dict(plan["signal"]),
                    new_events,
                    stage="preopen_revalidation",
                )
            except Exception as exc:
                reason = f"pre-open model revalidation failed closed: {type(exc).__name__}: {exc}"
                self.plans.set_plan_status(
                    str(plan["plan_id"]),
                    "invalidated",
                    reason=reason,
                    now=decision_time,
                )
                skipped.append({"ticker": plan["ticker"], "reason": reason})
                continue
            challenge = assessment.get("challenge") or {}
            if assessment.get("fail_closed") or challenge.get("veto_recommended"):
                reason = str(
                    assessment.get("failure_reason")
                    or "new evidence invalidated the active plan"
                )
                self.plans.set_plan_status(
                    str(plan["plan_id"]),
                    "invalidated",
                    reason=reason,
                    now=decision_time,
                )
                skipped.append({"ticker": plan["ticker"], "reason": reason})
            else:
                updated = self.plans.save_plan(
                    {
                        **plan,
                        "stage": "preopen_revalidation",
                        "preopen_revalidated_at": decision_time,
                        "snapshot": snapshot,
                        "evidence_snapshot": evidence_snapshot,
                        "updated_at": decision_time,
                        "revalidation": {
                            "decision_time": decision_time,
                            "snapshot_id": snapshot["snapshot_id"],
                            "veto_recommended": False,
                        },
                    }
                )
                plans.append(updated)
            append_jsonl(
                self.root,
                f"strategy_sleeves/{self.namespace}/decisions.jsonl",
                {
                    **assessment,
                    "decision_time": snapshot["decision_time"],
                    "data_cutoff_time": snapshot["data_cutoff_time"],
                    "evidence_snapshot": evidence_snapshot,
                    "prior_plan_id": plan["plan_id"],
                    "revalidation_only": True,
                    **uncalibrated_metadata(str(plan["signal"]["horizon"])),
                },
            )
            self.evidence.mark_researched(
                f"allocator:{plan['ticker']}",
                new_events,
                decision_time,
            )
        return {"plans": plans, "executions": [], "skipped": skipped}

    def _refresh_plan_evidence(
        self,
        plan: dict[str, Any],
        decision_time: str,
        *,
        stage: str,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]] | None:
        ticker = str(plan["ticker"]).upper()
        events, sources = self.news.search(ticker, decision_time)
        normalized = self.evidence.normalize_events(events, ticker=ticker)
        prior_snapshot = dict(plan["snapshot"])
        prior_events = self.evidence.normalize_events(
            list(prior_snapshot.get("available_news", [])),
            ticker=ticker,
        )
        seen = {
            str(event.get(key))
            for event in prior_events
            for key in ("canonical_url", "event_fingerprint", "content_hash")
            if event.get(key)
        }
        new_events = [
            event
            for event in normalized
            if not {
                str(event.get(key))
                for key in ("canonical_url", "event_fingerprint", "content_hash")
                if event.get(key)
            }
            & seen
        ]
        if not new_events:
            return None
        combined_events = self.evidence.normalize_events(
            [*new_events, *prior_events],
            ticker=ticker,
        )
        snapshot = {
            **prior_snapshot,
            "snapshot_id": f"allocator_{stage}_{ticker}_{uuid4().hex}",
            "decision_time": decision_time,
            "data_cutoff_time": decision_time,
            "market_session": self.clock.status(decision_time).market_session,
            "available_news": combined_events,
            "source_metadata": [
                *sources,
                *list(prior_snapshot.get("source_metadata", [])),
            ],
        }
        evidence_snapshot = self.evidence.write_snapshot(
            snapshot_type=f"allocator-{stage}-{ticker}",
            decision_time=decision_time,
            payload={
                "prior_plan_id": plan["plan_id"],
                "new_events": new_events,
                "events": combined_events,
                "source_metadata": snapshot["source_metadata"],
            },
        )
        return snapshot, new_events, evidence_snapshot

    def _research_stage(self, stage: str, decision_time: str) -> dict[str, Any]:
        skipped: list[dict[str, Any]] = []
        plans: list[dict[str, Any]] = []
        executions: list[dict[str, Any]] = []
        try:
            seeds = self.discovery.collect_seed_candidates(
                decision_time,
                list(self.config.get("universe", {}).get("default_watchlist", [])),
            )
            seed_by_ticker = {
                str(item["ticker"]).upper(): item
                for item in seeds
                if item.get("ticker")
            }
            occupied = set(self.broker.store.positions())
            occupied.update(
                position.contract.underlying.upper()
                for position in self.option_broker.store.positions().values()
            )
            blocked = occupied | self._same_session_stop_tickers(decision_time)
            seed_by_ticker = {
                ticker: item
                for ticker, item in seed_by_ticker.items()
                if ticker not in blocked
            }
            contexts = self.discovery.fetch_market_context(
                list(seed_by_ticker),
                decision_time,
            )
            candidates = self._technical_candidates(
                contexts,
                seed_by_ticker,
                decision_time,
            )
            selected = self._select_technical_candidates(
                candidates,
                int(self.profile.get("top_technical_candidates", 8)),
            )
        except Exception as exc:
            return {
                "plans": [],
                "executions": [],
                "skipped": [
                    {"stage": "discovery", "reason": f"{type(exc).__name__}: {exc}"}
                ],
            }

        researched: list[dict[str, Any]] = []
        for candidate in selected:
            ticker = str(candidate["ticker"])
            try:
                instrument = self.discovery.validate_instrument(ticker)
                if not instrument.get("valid", False):
                    skipped.append({"ticker": ticker, "reason": instrument.get("reason")})
                    continue
                events, sources = self.news.search(
                    ticker,
                    decision_time,
                    company_name=instrument.get("name"),
                )
                normalized = self.evidence.normalize_events(events, ticker=ticker)
                snapshot_ref = self.evidence.write_snapshot(
                    snapshot_type=f"allocator-{stage}-{ticker}",
                    decision_time=decision_time,
                    payload={
                        "candidate": candidate,
                        "instrument": instrument,
                        "events": normalized,
                        "source_metadata": sources,
                    },
                )
                eligible, model_events, reason = self.evidence.research_eligibility(
                    f"allocator:{ticker}",
                    normalized,
                    decision_time,
                    ticker_cooldown_minutes=int(
                        self.profile.get("ticker_cooldown_minutes", 120)
                    ),
                    event_cooldown_hours=int(
                        self.profile.get("event_cooldown_hours", 24)
                    ),
                )
                if not eligible:
                    skipped.append({"ticker": ticker, "reason": reason})
                    continue
                researched.append(
                    {
                        **candidate,
                        "events": model_events,
                        "source_metadata": sources,
                        "evidence_snapshot": snapshot_ref,
                        "instrument": instrument,
                        "research_time": decision_time,
                        "market_session": self.clock.status(decision_time).market_session,
                    }
                )
            except Exception as exc:
                skipped.append(
                    {"ticker": ticker, "reason": f"research failed closed: {type(exc).__name__}: {exc}"}
                )

        if not researched:
            return {"plans": plans, "executions": executions, "skipped": skipped}
        cycle_id = f"allocator_{uuid4().hex}"
        try:
            ranking = self.team.rank(
                snapshot_id=cycle_id,
                decision_time=decision_time,
                candidates=[self._ranking_payload(item) for item in researched],
            )
        except (ProviderError, ValueError) as exc:
            skipped.append({"stage": "ranking", "reason": str(exc)})
            return {"plans": plans, "executions": executions, "skipped": skipped}
        for item in researched:
            self.evidence.mark_researched(
                f"allocator:{item['ticker']}",
                item["events"],
                decision_time,
            )
        ranked = self._validated_ranking(ranking, researched)
        for rank in ranked[: int(self.profile.get("top_deep_research_candidates", 3))]:
            item = next(value for value in researched if value["ticker"] == rank["ticker"])
            snapshot = self._agent_snapshot(item, rank)
            analysis = self.team.analyze(snapshot, rank, stage=stage)
            signal = analysis["signal"]
            record = {
                **analysis,
                "decision_time": snapshot["decision_time"],
                "data_cutoff_time": snapshot["data_cutoff_time"],
                "evidence_snapshot": item["evidence_snapshot"],
                **uncalibrated_metadata(str(signal["horizon"])),
            }
            append_jsonl(
                self.root,
                f"strategy_sleeves/{self.namespace}/decisions.jsonl",
                record,
            )
            actionable = not analysis.get("fail_closed") and signal.get(
                "action"
            ) == "propose_trade"
            for existing in self.plans.active_plans(snapshot["decision_time"]):
                if existing["ticker"] != item["ticker"]:
                    continue
                self.plans.set_plan_status(
                    str(existing["plan_id"]),
                    "superseded" if actionable else "invalidated",
                    reason=f"replaced by newer {stage} analysis",
                    now=snapshot["decision_time"],
                )
            if not actionable:
                skipped.append(
                    {"ticker": item["ticker"], "reason": signal.get("no_trade_reason")}
                )
                continue
            plan = self.plans.save_plan(
                {
                    "plan_id": f"plan_{uuid4().hex}",
                    "strategy": self.STRATEGY,
                    "ticker": item["ticker"],
                    "created_at": snapshot["decision_time"],
                    "valid_until": signal["thesis_valid_until"],
                    "status": "active",
                    "stage": stage,
                    "signal": signal,
                    "snapshot": snapshot,
                    "ranking": rank,
                    "evidence_snapshot": item["evidence_snapshot"],
                }
            )
            plans.append(plan)
            if stage == "intraday":
                executions.append(self._execute_plan(plan, decision_time, stage=stage))
        return {"plans": plans, "executions": executions, "skipped": skipped}

    def _execute_plan(
        self,
        plan: dict[str, Any],
        now: str,
        *,
        stage: str,
    ) -> dict[str, Any]:
        if stage not in self.EXECUTION_STAGES:
            return {"status": "no_trade", "reason": "research stage cannot create orders", "order": None}
        try:
            ticker = str(plan["ticker"]).upper()
            raw_signal = plan["signal"]
            if not isinstance(raw_signal, dict):
                raise TypeError("plan signal must be an object")
            signal = dict(raw_signal)
            validate_actionable_signal(signal, now)
        except (KeyError, TypeError, ValueError) as exc:
            return {
                "status": "no_trade",
                "reason": f"invalid actionable signal: {exc}",
                "order": None,
            }
        conditional_plan = plan.get("stage") in self.RESEARCH_STAGES - {"intraday"}
        if stage == "open_execution" and not conditional_plan:
            return {
                "status": "no_trade",
                "reason": "plan source is not eligible for open execution",
                "order": None,
            }
        if stage == "open_execution":
            revalidated_at = plan.get("preopen_revalidated_at")
            clock = self.clock.status(now)
            try:
                current_revalidation = bool(revalidated_at) and (
                    parse_ts(str(revalidated_at)).date()
                    == parse_ts(str(clock.open_time)).date()
                    and parse_ts(str(revalidated_at)) <= parse_ts(now)
                )
            except (TypeError, ValueError):
                current_revalidation = False
            if not current_revalidation:
                return {
                    "status": "no_trade",
                    "reason": "current pre-open revalidation is required",
                    "order": None,
                }
        if stage == "intraday" and not signal.get("entry_now", False):
            return {"status": "no_trade", "reason": "model did not authorize entry", "order": None}
        try:
            quote = self.discovery.fetch_current_quote(
                ticker,
                average_daily_volume_usd=None,
            )
            summary = derive_signal_summary(signal)
            option_type = "call" if summary["direction"] == "bullish" else "put"
            dte = self.profile.get("option_dte_by_horizon", {}).get(
                signal["horizon"],
                {},
            )
            account_state = self._account_state(now)
            max_premium = float(account_state["nav_usd"]) * float(
                self.config["options_risk"].get("max_order_risk_pct_of_equity", 0.03)
            )
            option_candidates, option_diagnostics = self.option_data.fetch_contract_candidates(
                underlying=ticker,
                underlying_price=quote.last,
                option_type=option_type,
                now=now,
                min_dte=int(dte.get("min_dte", self.config["options_universe"]["min_dte"])),
                target_dte=int(dte.get("target_dte", self.config["options_universe"]["target_dte"])),
                max_dte=int(dte.get("max_dte", self.config["options_universe"]["max_dte"])),
                max_premium_usd=max_premium,
            )
        except Exception as exc:
            return {
                "status": "no_trade",
                "reason": f"fresh executable data failed closed: {type(exc).__name__}: {exc}",
                "order": None,
            }
        try:
            execution_now = max(
                [
                    parse_ts(now),
                    parse_ts(quote.asof),
                    *(
                        parse_ts(option_quote.updated_at)
                        for _, option_quote in option_candidates
                    ),
                ]
            ).isoformat()
            planned_exit_at = planned_exit_time(
                execution_now,
                signal["horizon"],
                max_holding_trading_days=int(
                    signal.get("max_holding_trading_days", 1)
                ),
                minutes_before_close=int(
                    self.config["paper"].get("exit_before_close_minutes", 10)
                ),
            )
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            return {
                "status": "no_trade",
                "reason": f"fresh executable data failed closed: {type(exc).__name__}: {exc}",
                "order": None,
            }
        allocation = allocate_instrument(
            signal,
            quote,
            option_candidates,
            account_state,
            self.config,
            execution_now,
            planned_exit_at=planned_exit_at,
        )
        allocation.update(
            {
                "plan_id": plan["plan_id"],
                "decision_time": execution_now,
                "data_cutoff_time": execution_now,
                "entry_authorization_valid_until": (
                    parse_ts(execution_now)
                    + timedelta(
                        seconds=int(self.profile.get("max_entry_validity_seconds", 300))
                    )
                ).isoformat(),
                "option_candidate_diagnostics": option_diagnostics,
            }
        )
        self.plans.record_allocation(allocation)
        append_jsonl(
            self.root,
            f"strategy_sleeves/{self.namespace}/allocations.jsonl",
            allocation,
        )
        if allocation.get("short_equity_counterfactual"):
            append_jsonl(
                self.root,
                f"strategy_sleeves/{self.namespace}/short_equity_counterfactual.jsonl",
                {
                    "decision_time": execution_now,
                    "plan_id": plan["plan_id"],
                    **allocation["short_equity_counterfactual"],
                },
            )
        selected = allocation.get("selected_instrument")
        if allocation.get("status") != "selected" or not isinstance(selected, dict):
            return {"status": "no_trade", "reason": allocation.get("reason"), "allocation": allocation, "order": None}

        if selected["instrument_type"] == "equity":
            order = self.broker.create_order(
                decision_id=allocation["allocation_id"],
                symbol=ticker,
                side="buy",
                order_type="limit",
                quantity=float(selected["quantity"]),
                limit_price=float(selected["entry_price"]),
                quote_seen_at=quote.asof,
                thesis=signal["thesis"],
                strategy=self.STRATEGY,
                planned_stop_price=float(selected["planned_stop_price"]),
                signal_horizon=signal["horizon"],
                idempotency_key=f"{self.STRATEGY}:{plan['plan_id']}:equity:{ticker}",
                now=execution_now,
            )
            exposure_id = f"equity:{ticker}"
            self._register_mandate(order.order_id, exposure_id, selected["instrument_type"], signal, plan, planned_exit_at, selected.get("planned_stop_price"), execution_now)
            submitted = self.broker.submit_order(
                order,
                quote,
                execution_now,
                entry_nav_usd=float(account_state["nav_usd"]),
            )
        else:
            candidate = next(
                (
                    (contract, option_quote)
                    for contract, option_quote in option_candidates
                    if contract.option_id == selected["option_id"]
                ),
                None,
            )
            if candidate is None:
                return {"status": "no_trade", "reason": "selected option disappeared before order creation", "allocation": allocation, "order": None}
            contract, option_quote = candidate
            order = self.option_broker.create_order(
                decision_id=allocation["allocation_id"],
                contract=contract,
                intent="buy_to_open",
                order_type="limit",
                quantity=int(selected["quantity"]),
                limit_price=float(selected["entry_price"]),
                quote_seen_at=option_quote.updated_at,
                thesis=signal["thesis"],
                strategy=self.STRATEGY,
                signal_horizon=signal["horizon"],
                idempotency_key=f"{self.STRATEGY}:{plan['plan_id']}:option:{contract.option_id}",
                now=execution_now,
            )
            exposure_id = f"option:{contract.option_id}"
            self._register_mandate(order.order_id, exposure_id, selected["instrument_type"], signal, plan, planned_exit_at, None, execution_now)
            submitted = self.option_broker.submit_order(
                order,
                option_quote,
                execution_now,
                entry_nav_usd=float(account_state["nav_usd"]),
            )
        self.mandates.reconcile(
            equity_orders=self.broker.store.orders(),
            option_orders=self.option_broker.store.orders(),
            now=execution_now,
        )
        self.plans.set_plan_status(
            str(plan["plan_id"]),
            "executed" if submitted.status in {"filled", "open", "partially_filled"} else "rejected",
            reason=submitted.reject_reason,
            now=execution_now,
        )
        return {
            "status": submitted.status,
            "reason": submitted.reject_reason,
            "allocation": allocation,
            "order": submitted.to_dict(),
            "live_order_tools_called": False,
        }

    def _register_mandate(
        self,
        order_id: str,
        exposure_id: str,
        instrument_type: str,
        signal: dict[str, Any],
        plan: dict[str, Any],
        planned_exit_at: str,
        planned_stop_price: float | None,
        now: str,
    ) -> None:
        self.mandates.register_order(
            order_id=order_id,
            exposure_id=exposure_id,
            strategy=self.STRATEGY,
            snapshot_id=str(plan["snapshot"]["snapshot_id"]),
            ticker=str(plan["ticker"]),
            instrument_type=instrument_type,
            horizon=str(signal["horizon"]),
            max_holding_trading_days=int(signal["max_holding_trading_days"]),
            created_at=now,
            planned_exit_at=planned_exit_at,
            thesis_valid_until=str(signal["thesis_valid_until"]),
            invalidation_condition=str(signal["invalidation_condition"]),
            planned_stop_price=planned_stop_price,
        )

    def _recover_allocator_entry_orders(
        self,
        now: str,
        *,
        cancel_retryable_reason: str | None = None,
    ) -> list[dict[str, Any]]:
        entries: list[tuple[Any, Any, str, str, str]] = []
        for order in self.broker.store.orders().values():
            if order.strategy == self.STRATEGY and order.side == "buy":
                entries.append(
                    (
                        self.broker,
                        order,
                        f"equity:{order.symbol}",
                        order.symbol,
                        "equity",
                    )
                )
        for order in self.option_broker.store.orders().values():
            if order.strategy == self.STRATEGY and order.intent == "buy_to_open":
                entries.append(
                    (
                        self.option_broker,
                        order,
                        f"option:{order.contract.option_id}",
                        order.contract.underlying,
                        order.contract.option_type,
                    )
                )

        updates: list[dict[str, Any]] = []
        retryable = {"submitted_to_paper_broker", "open", "partially_filled"}
        for broker, order, exposure_id, ticker, instrument_type in entries:
            reason = None
            if order.status == "created":
                reason = "allocator created entry cancelled during restart recovery"
            elif order.status in retryable:
                mandate = self.mandates.for_exposure(exposure_id)
                if cancel_retryable_reason:
                    reason = cancel_retryable_reason
                elif not validate_position_mandate(
                    mandate,
                    allowed_statuses={"pending_fill", "open"},
                    expected_exposure_id=exposure_id,
                    expected_ticker=ticker,
                    expected_instrument_type=instrument_type,
                    expected_order_id=order.order_id,
                    expected_strategy=self.STRATEGY,
                ):
                    reason = (
                        "allocator entry mandate invalid during restart recovery"
                    )
            if reason is None:
                continue

            allocation = self.plans.allocations().get(str(order.decision_id), {})
            plan_id = allocation.get("plan_id")
            if plan_id:
                self.plans.set_plan_status(
                    str(plan_id),
                    "invalidated",
                    reason=reason,
                    now=now,
                )
            cancelled = broker.cancel_order(order.order_id, reason=reason, now=now)
            for mandate_key, mandate in self.mandates.mandates().items():
                if (
                    mandate_key == exposure_id
                    or str(mandate.get("order_id")) == order.order_id
                ):
                    self.mandates.close(mandate_key, reason=reason, now=now)
            updates.append(
                {
                    "event": "allocator_entry_recovered_fail_closed",
                    "reason": reason,
                    "exposure_id": exposure_id,
                    "order": cancelled.to_dict(),
                }
            )
        return updates

    def _account_state(
        self,
        now: str,
        *,
        equity_quotes: dict[str, Quote] | None = None,
        option_quotes: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        account = self.broker.store.account()
        equity_positions = self.broker.store.positions()
        option_positions = self.option_broker.store.positions()
        marks = dict(equity_quotes or {})
        if equity_quotes is None:
            for symbol, position in equity_positions.items():
                marks[symbol] = self.discovery.fetch_current_quote(
                    symbol,
                    average_daily_volume_usd=position.average_price * 1_000_000,
                )
        option_marks = dict(option_quotes or {})
        if option_quotes is None and option_positions:
            option_marks = self.option_data.fetch_quotes(sorted(option_positions))

        nav = float(account.cash)
        equity_mark_times: dict[str, str] = {}
        option_mark_times: dict[str, str] = {}
        for symbol, position in equity_positions.items():
            quote = marks.get(symbol)
            validation = validate_quote(
                quote,
                now,
                int(self.config["paper"].get("quote_stale_after_seconds", 60)),
                self.config["universe"],
                enforce_entry_liquidity=False,
            )
            if not validation.approved or quote is None:
                raise ValueError(
                    f"marked NAV unavailable for equity {symbol}: {validation.reason}"
                )
            if quote.symbol.upper() != symbol.upper():
                raise ValueError(
                    f"marked NAV unavailable for equity {symbol}: quote identity mismatch"
                )
            nav += float(position.quantity) * float(quote.bid)
            equity_mark_times[symbol] = quote.asof
        for option_id, position in option_positions.items():
            quote = option_marks.get(option_id)
            validation = validate_option_quote(
                quote,
                now,
                self.config,
                enforce_entry_liquidity=False,
            )
            if not validation.approved or quote is None:
                raise ValueError(
                    f"marked NAV unavailable for option {option_id}: {validation.reason}"
                )
            if quote.option_id != option_id:
                raise ValueError(
                    f"marked NAV unavailable for option {option_id}: quote identity mismatch"
                )
            nav += (
                float(position.quantity)
                * float(quote.bid)
                * float(position.contract.multiplier)
            )
            option_mark_times[option_id] = quote.updated_at

        if not math.isfinite(nav) or nav <= 0:
            raise ValueError("marked NAV is non-positive or non-finite")

        deployment = shared_deployment(
            account,
            equity_positions,
            option_positions,
            self.broker.store.orders(),
            self.option_broker.store.orders(),
        )
        return {
            "cash_usd": float(account.cash),
            "nav_usd": nav,
            "nav_valuation_method": "conservative_liquidation_bid_v1",
            "nav_calculated_at": now,
            "equity_mark_times": equity_mark_times,
            "option_mark_times": option_mark_times,
            "equity_deployed_usd": float(deployment["equity_deployed"]),
            "options_deployed_usd": float(deployment["options_deployed"]),
        }

    def _stage_session_allowed(self, stage: str, now: str) -> bool:
        clock = self.clock.status(now)
        session = clock.market_session
        if stage == "open_execution":
            if session != "regular" or not clock.open_time:
                return False
            minutes_after_open = (
                parse_ts(now) - parse_ts(clock.open_time)
            ).total_seconds() / 60
            start = float(
                self.profile.get("open_execution_start_minutes_after_open", 2)
            )
            window = float(self.profile.get("open_execution_window_minutes", 5))
            return start <= minutes_after_open <= start + window
        if stage == "intraday":
            return session == "regular"
        if stage in {"premarket_update", "preopen_revalidation"}:
            return session == "pre_market"
        return session in {"after_hours", "closed"}

    def _stage_result(
        self,
        stage: str,
        decision_time: str,
        *,
        reason: str | None = None,
        plans: list[dict[str, Any]] | None = None,
        executions: list[dict[str, Any]] | None = None,
        skipped: list[dict[str, Any]] | None = None,
        model_calls: int = 0,
        monitor: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        plans = plans or []
        executions = executions or []
        result = {
            "event": "ai_instrument_allocator_stage_complete",
            "strategy": self.STRATEGY,
            "stage": stage,
            "decision_time": decision_time,
            "reason": reason,
            "plans": plans,
            "executions": executions,
            "skipped": skipped or [],
            "model_calls": model_calls,
            "paper_orders_created": sum(
                1 for item in executions if isinstance(item.get("order"), dict)
            ),
            "paper_sleeve": self.namespace,
            "paper_initial_cash_usd": float(
                self.profile.get("paper_initial_cash_usd", 10_000)
            ),
            "monitor": monitor,
            "live_order_tools_called": False,
        }
        append_jsonl(
            self.root,
            f"strategy_sleeves/{self.namespace}/cycles.jsonl",
            result,
        )
        return result
