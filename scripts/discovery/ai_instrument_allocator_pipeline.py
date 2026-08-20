from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from scripts.agents.ai_instrument_allocator_team import AiInstrumentAllocatorTeam
from scripts.core.audit import append_jsonl
from scripts.core.models import Quote, parse_ts, utc_now
from scripts.decision.instrument_allocator import allocate_instrument
from scripts.decision.signed_return_signal import derive_signal_summary
from scripts.discovery.ai_gated_pipeline import AiGatedPaperPipeline
from scripts.discovery.evidence_store import EvidenceSnapshotStore
from scripts.exit.evaluate_exit import evaluate_position_exit
from scripts.exit.position_mandates import (
    PositionMandateStore,
    evaluate_mandate_exit,
    planned_exit_time,
)
from scripts.evaluation.probability_calibration import uncalibrated_metadata
from scripts.journal.write_trade_journal import write_order_journal
from scripts.llm.base_provider import LLMProvider, ProviderError
from scripts.llm.usage_tracker import UsageTracker
from scripts.options.exit_policy import evaluate_option_exit
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
        self.mandates.reconcile(
            equity_orders=self.broker.store.orders(),
            option_orders=self.option_broker.store.orders(),
            now=decision_time,
        )
        if not clock.is_regular:
            return {
                "event": "ai_instrument_allocator_monitor_idle",
                "reason": f"market session is {clock.market_session}",
                "live_order_tools_called": False,
            }

        positions = self.broker.store.positions()
        option_positions = self.option_broker.store.positions()
        quotes: dict[str, Quote] = {}
        option_quotes: dict[str, Any] = {}
        errors: dict[str, str] = {}
        for symbol, position in positions.items():
            try:
                quotes[symbol] = self.discovery.fetch_current_quote(
                    symbol,
                    average_daily_volume_usd=position.average_price * 1_000_000,
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

        open_updates = [
            order.to_dict()
            for order in self.broker.process_open_orders(quotes, decision_time)
        ]
        option_open_updates = [
            order.to_dict()
            for order in self.option_broker.process_open_orders(option_quotes, decision_time)
        ]
        self.mandates.reconcile(
            equity_orders=self.broker.store.orders(),
            option_orders=self.option_broker.store.orders(),
            now=decision_time,
        )

        exits: list[dict[str, Any]] = []
        for symbol, position in list(self.broker.store.positions().items()):
            quote = quotes.get(symbol)
            mandate = self.mandates.for_exposure(f"equity:{symbol}")
            mandate_exit = evaluate_mandate_exit(mandate, decision_time)
            price_exit = evaluate_position_exit(
                position,
                quote,
                decision_time,
                self.config["risk"],
                minutes_to_close=None,
                exit_before_close_minutes=int(
                    self.config["paper"].get("exit_before_close_minutes", 10)
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
            mandate = self.mandates.for_exposure(f"option:{option_id}")
            mandate_exit = evaluate_mandate_exit(mandate, decision_time)
            price_exit = evaluate_option_exit(
                position,
                quote,
                decision_time,
                self.config["options_risk"],
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
        portfolio = {
            "event": "ai_instrument_allocator_portfolio_snapshot",
            "asof": decision_time,
            "cash": round(account.cash, 6),
            "initial_cash": account.initial_cash,
            **deployment,
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
            "quote_errors": errors,
            "portfolio": portfolio,
            "live_order_tools_called": False,
        }

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
            if analysis.get("fail_closed") or signal.get("action") != "propose_trade":
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
            self.evidence.mark_researched(
                f"allocator:{item['ticker']}",
                item["events"],
                snapshot["decision_time"],
            )
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
        ticker = str(plan["ticker"]).upper()
        signal = dict(plan["signal"])
        if not signal.get("entry_now", False):
            return {"status": "no_trade", "reason": "model did not authorize entry", "order": None}
        try:
            quote = self.discovery.fetch_current_quote(ticker)
            summary = derive_signal_summary(signal)
            option_type = "call" if summary["direction"] == "bullish" else "put"
            dte = self.profile.get("option_dte_by_horizon", {}).get(
                signal["horizon"],
                {},
            )
            account_state = self._account_state()
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
        allocation = allocate_instrument(
            signal,
            quote,
            option_candidates,
            account_state,
            self.config,
            now,
        )
        allocation.update(
            {
                "plan_id": plan["plan_id"],
                "decision_time": now,
                "data_cutoff_time": max(parse_ts(now), parse_ts(quote.asof)).isoformat(),
                "entry_authorization_valid_until": (
                    parse_ts(now)
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
                    "decision_time": now,
                    "plan_id": plan["plan_id"],
                    **allocation["short_equity_counterfactual"],
                },
            )
        selected = allocation.get("selected_instrument")
        if allocation.get("status") != "selected" or not isinstance(selected, dict):
            return {"status": "no_trade", "reason": allocation.get("reason"), "allocation": allocation, "order": None}

        planned_exit_at = planned_exit_time(
            now,
            signal["horizon"],
            max_holding_trading_days=int(signal.get("max_holding_trading_days", 1)),
            minutes_before_close=int(self.config["paper"].get("exit_before_close_minutes", 10)),
        )
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
                now=now,
            )
            exposure_id = f"equity:{ticker}"
            self._register_mandate(order.order_id, exposure_id, selected["instrument_type"], signal, plan, planned_exit_at, selected.get("planned_stop_price"), now)
            submitted = self.broker.submit_order(order, quote, now)
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
                now=now,
            )
            exposure_id = f"option:{contract.option_id}"
            self._register_mandate(order.order_id, exposure_id, selected["instrument_type"], signal, plan, planned_exit_at, None, now)
            submitted = self.option_broker.submit_order(order, option_quote, now)
        self.mandates.reconcile(
            equity_orders=self.broker.store.orders(),
            option_orders=self.option_broker.store.orders(),
            now=now,
        )
        self.plans.set_plan_status(
            str(plan["plan_id"]),
            "executed" if submitted.status in {"filled", "open", "partially_filled"} else "rejected",
            reason=submitted.reject_reason,
            now=now,
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
            created_at=now,
            planned_exit_at=planned_exit_at,
            thesis_valid_until=str(signal["thesis_valid_until"]),
            invalidation_condition=str(signal["invalidation_condition"]),
            planned_stop_price=planned_stop_price,
        )

    def _account_state(self) -> dict[str, float]:
        account = self.broker.store.account()
        deployment = shared_deployment(
            account,
            self.broker.store.positions(),
            self.option_broker.store.positions(),
            self.broker.store.orders(),
            self.option_broker.store.orders(),
        )
        return {
            "cash_usd": float(account.cash),
            "nav_usd": float(deployment["account_equity_at_cost"]),
            "equity_deployed_usd": float(deployment["equity_deployed"]),
            "options_deployed_usd": float(deployment["options_deployed"]),
        }

    def _stage_session_allowed(self, stage: str, now: str) -> bool:
        session = self.clock.status(now).market_session
        if stage in {"open_execution", "intraday"}:
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
