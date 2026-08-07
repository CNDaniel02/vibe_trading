from __future__ import annotations

import argparse
import json
import time
import os
import signal
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from apscheduler.schedulers.blocking import BlockingScheduler

from scripts.adapters.alpaca_market_data_adapter import AlpacaMarketDataAdapter
from scripts.adapters.errors import AdapterError
from scripts.adapters.exa_news_adapter import ExaNewsAdapter
from scripts.adapters.robinhood_mcp_market_data_adapter import RobinhoodMcpMarketDataAdapter
from scripts.adapters.robinhood_option_market_data_adapter import RobinhoodOptionMarketDataAdapter
from scripts.adapters.vibe_market_data_adapter import VibeMarketDataAdapter
from scripts.adapters.vibe_research_swarm_adapter import VibeResearchSwarmAdapter
from scripts.agents.api_investment_team import ApiInvestmentTeam
from scripts.core.audit import append_jsonl
from scripts.core.config import assert_paper_mode, load_runtime_config
from scripts.core.models import Quote, parse_ts, utc_now
from scripts.discovery.catalyst_pipeline import CatalystDiscoveryPipeline
from scripts.discovery.ai_gated_pipeline import AiGatedPaperPipeline
from scripts.discovery.catalyst_signal_store import CatalystSignalStore
from scripts.exit.evaluate_exit import evaluate_position_exit
from scripts.evaluation.outcome_labeler import CandidateOutcomeLabeler
from scripts.evaluation.calculate_metrics import calculate_metrics
from scripts.evaluation.generate_performance_report import generate_report
from scripts.evaluation.evaluate_news_drift import (
    calculate_news_drift_metrics,
    generate_news_drift_report,
)
from scripts.journal.write_trade_journal import write_order_journal
from scripts.llm import build_provider
from scripts.news_drift.pipeline import NewsDriftPipeline
from scripts.research.snapshot_builder import build_snapshot
from scripts.risk.position_sizing import calculate_entry_quantity
from scripts.risk.shared_portfolio_risk import shared_entry_capacity
from scripts.runtime.heartbeat import write_heartbeat
from scripts.runtime.market_clock import UsEquityMarketClock
from scripts.runtime.process_lock import ProcessLock
from scripts.options.exit_policy import evaluate_option_exit
from scripts.options.paper_broker import OptionPaperBroker
from scripts.options.portfolio import aggregate_portfolio_greeks
from scripts.options.strategy import decide_option_direction
from scripts.options.weighted_strategy import decide_weighted_option_direction
from scripts.runtime.subprocess_runner import SubprocessJobRunner
from scripts.simulation.paper_broker import PaperBroker
from scripts.strategies.relative_strength_v1 import decide_snapshot
from scripts.strategies.weighted_relative_strength_v2 import decide_snapshot as decide_weighted_snapshot


def run_news_drift_once(root: str | Path, now: str | None = None) -> dict[str, Any]:
    """Run the isolated shadow lane without constructing either paper account."""
    root_path = Path(root).resolve()
    config = load_runtime_config(root_path)
    assert_paper_mode(config)
    provider, tracker = build_provider(config["llm"], root_path)
    news = ExaNewsAdapter(config.get("integrations", {}).get("forward_data", {}).get("exa", {}))
    result = NewsDriftPipeline(
        root_path,
        config,
        provider,
        tracker,
        news_adapter=news,
    ).run(now)
    append_jsonl(
        root_path,
        "audit.jsonl",
        {
            "event": result.get("event"),
            "strategy": "llm_news_drift_v1",
            "cycle_id": result.get("cycle_id"),
            "signal_count": len(result.get("signals", [])),
            "proposal_count": len(result.get("proposals", [])),
            "paper_orders_created": 0,
            "live_order_tools_called": False,
        },
    )
    return result


class ForwardPaperService:
    """Continuous paper-only service using read-only real market observations."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.config = load_runtime_config(self.root)
        assert_paper_mode(self.config)
        integrations = self.config.get("integrations", {})
        self.integration_config = integrations
        self.vibe = VibeMarketDataAdapter(self.root, integrations.get("vibe", {}))
        forward = integrations.get("forward_data", {})
        self.quote_provider = str(forward.get("quote_provider", "alpaca"))
        self.quote_adapter = self._build_quote_adapter(self.quote_provider, forward, integrations)
        configured_fallback = str(forward.get("fallback_quote_provider", "")).strip()
        self.fallback_quote_provider = (
            configured_fallback
            if configured_fallback and configured_fallback != self.quote_provider
            else None
        )
        self.fallback_quote_adapter = (
            self._build_quote_adapter(self.fallback_quote_provider, forward, integrations)
            if self.fallback_quote_provider
            else None
        )
        self.news_adapter = ExaNewsAdapter(forward.get("exa", {}))
        self.swarm = VibeResearchSwarmAdapter(self.root, integrations.get("vibe", {}))
        self.clock = UsEquityMarketClock()
        self.broker = PaperBroker(self.root, self.config)
        self.option_broker = OptionPaperBroker(self.root, self.config)
        self.option_data = RobinhoodOptionMarketDataAdapter(integrations.get("robinhood_mcp", {}), self.config, self.root)
        provider, tracker = build_provider(self.config["llm"], self.root)
        self.shadow_team = ApiInvestmentTeam(self.root, self.config, provider, tracker)
        self.catalyst_pipeline = CatalystDiscoveryPipeline(
            self.root,
            self.config,
            provider,
            tracker,
            news_adapter=self.news_adapter,
            option_data=self.option_data,
        )
        self.ai_gated_pipeline = AiGatedPaperPipeline(
            self.root,
            self.config,
            provider,
            tracker,
            news_adapter=self.news_adapter,
            option_data=self.option_data,
        )
        self.catalyst_signals = CatalystSignalStore(self.root)
        self.outcome_labeler = CandidateOutcomeLabeler(
            self.root,
            self.config.get("strategies", {}).get("weighted_relative_strength_v2", {}),
            self.config.get("costs", {}),
        )
        self.tracker = tracker

    def readiness(self) -> dict[str, Any]:
        vibe_status = self.vibe.runtime.status().to_dict()
        llm_provider = str(self.config["llm"].get("provider", "mock"))
        llm_key_env = str(self.config["llm"].get("api", {}).get("api_key_env", "OPENAI_API_KEY"))
        llm_ready = llm_provider == "mock" or bool(os.getenv(llm_key_env))
        quote_provider = self.quote_adapter.readiness()
        fallback_quote_data = (
            self.fallback_quote_adapter.readiness()
            if self.fallback_quote_adapter is not None
            else None
        )
        quote_ready = bool(
            quote_provider.get("ready")
            or (fallback_quote_data and fallback_quote_data.get("ready"))
        )
        option_data = self.option_data.readiness()
        discovery_data = self.catalyst_pipeline.discovery.readiness()
        exa = self.news_adapter.readiness()
        return {
            "paper_mode": True,
            "live_trading": False,
            "vibe": vibe_status,
            "quote_provider": self.quote_provider,
            "quote_data": quote_provider,
            "fallback_quote_provider": self.fallback_quote_provider,
            "fallback_quote_data": fallback_quote_data,
            "option_data": option_data,
            "discovery_data": discovery_data,
            "exa": exa,
            "llm_provider": llm_provider,
            "llm_api_key_env": llm_key_env if llm_provider == "api" else None,
            "llm_ready": llm_ready,
            "ready_for_forward_quotes": vibe_status["ready"] and quote_ready,
            "ready_for_news_shadow": exa["ready"] and llm_ready,
            "ready_for_catalyst_discovery": discovery_data["ready"] and exa["ready"] and llm_ready,
            "ready_for_ai_gated_paper": discovery_data["ready"] and exa["ready"] and llm_ready,
            "ready_for_news_drift_shadow": discovery_data["ready"] and exa["ready"] and llm_ready,
            "ready_for_full_forward_evaluation": vibe_status["ready"] and quote_ready and discovery_data["ready"] and option_data["ready"] and exa["ready"] and llm_ready,
        }

    def run_once(self, now: str | None = None) -> dict[str, Any]:
        requested_now = now
        cycle_now = requested_now or utc_now()
        cycle_id = f"forward_{uuid.uuid4().hex}"
        clock = self.clock.status(cycle_now)
        if not clock.is_regular:
            event = {"event": "forward_cycle_skipped", "reason": f"market session is {clock.market_session}", "clock": clock.to_dict()}
            append_jsonl(self.root, "audit.jsonl", event)
            write_heartbeat(self.root, "idle", event, now=cycle_now)
            return event

        equity_watchlist = list(dict.fromkeys(self.config["universe"].get("default_watchlist", [])))
        options_enabled = bool(self.config["paper"].get("strategy_lines", {}).get("options", False))
        option_watchlist = (
            list(dict.fromkeys(self.config["universe"].get("options_watchlist", equity_watchlist)))
            if options_enabled
            else []
        )
        analysis_watchlist = list(dict.fromkeys([*equity_watchlist, *option_watchlist]))
        equity_symbols = set(equity_watchlist)
        option_symbols = set(option_watchlist)
        symbols = list(dict.fromkeys([*analysis_watchlist, "SPY"]))
        etf_symbols = {
            str(symbol).upper()
            for symbol in self.config["universe"].get("etf_symbols", [])
        }
        try:
            self._record_forward_stage(cycle_id, cycle_now, "history", "started", symbols=len(symbols))
            bars = self.vibe.fetch_lookback(symbols, cycle_now)
            self._record_forward_stage(cycle_id, cycle_now, "history", "completed", symbols=len(bars))
            liquidity = {symbol: self.vibe.average_daily_volume_usd(bars[symbol], cycle_now) for symbol in symbols}
            asset_classes = {
                symbol: "us_etf" if symbol in etf_symbols else "us_equity"
                for symbol in symbols
            }
            quotes, effective_quote_adapter, effective_quote_provider = self._fetch_forward_quotes(
                cycle_id,
                cycle_now,
                symbols,
                liquidity,
                asset_classes,
            )
        except AdapterError as exc:
            event = {"event": "forward_cycle_failed_closed", "reason": str(exc), "stage": "market_data", "clock": clock.to_dict()}
            self._record_forward_stage(cycle_id, cycle_now, "market_data", "failed", reason=str(exc))
            append_jsonl(self.root, "audit.jsonl", event)
            write_heartbeat(self.root, "failed", event, now=cycle_now)
            return event

        # A live quote arrives after the cycle starts. Advance the decision
        # clock after data collection so valid observations are not rejected as
        # future-looking solely because network I/O took a few seconds. An
        # explicitly supplied timestamp remains fixed for deterministic replay.
        decision_now = requested_now or utc_now()
        clock = self.clock.status(decision_now)
        if not clock.is_regular:
            event = {
                "event": "forward_cycle_skipped",
                "reason": f"market session changed to {clock.market_session} during data collection",
                "clock": clock.to_dict(),
            }
            append_jsonl(self.root, "audit.jsonl", event)
            write_heartbeat(self.root, "idle", event, now=decision_now)
            return event

        if hasattr(effective_quote_adapter, "fetch_session_volumes") and clock.open_time:
            try:
                self._record_forward_stage(cycle_id, decision_now, "session_volume", "started", provider=effective_quote_provider)
                session_volumes = effective_quote_adapter.fetch_session_volumes(symbols, clock.open_time, decision_now)
                self._record_forward_stage(cycle_id, decision_now, "session_volume", "completed", provider=effective_quote_provider)
            except Exception as exc:
                session_volumes = {}
                self._record_forward_stage(
                    cycle_id,
                    decision_now,
                    "session_volume",
                    "failed",
                    provider=effective_quote_provider,
                    reason=f"{type(exc).__name__}: {exc}",
                )
                append_jsonl(
                    self.root,
                    "audit.jsonl",
                    {"event": "intraday_volume_failed_closed", "reason": f"{type(exc).__name__}: {exc}", "asof": decision_now},
                )
            for symbol, quote in quotes.items():
                quote.session_volume = session_volumes.get(symbol)
            if requested_now is None:
                decision_now = utc_now()
                clock = self.clock.status(decision_now)
                if not clock.is_regular:
                    event = {"event": "forward_cycle_skipped", "reason": "market session changed during intraday volume collection", "clock": clock.to_dict()}
                    append_jsonl(self.root, "audit.jsonl", event)
                    write_heartbeat(self.root, "idle", event, now=decision_now)
                    return event

        open_updates = [order.to_dict() for order in self.broker.process_open_orders(quotes, decision_now)]
        resolved_outcomes = self.outcome_labeler.resolve(quotes, decision_now)
        self.broker.lifecycle.mark_equity_quotes(quotes, decision_now)
        exits = self._process_exits(quotes, clock)
        option_quotes, option_data_error = self._fetch_held_option_quotes()
        option_monitor_now = utc_now(timespec="microseconds") if option_quotes else decision_now
        option_open_updates = [order.to_dict() for order in self.option_broker.process_open_orders(option_quotes, option_monitor_now)]
        self.option_broker.lifecycle.mark_option_quotes(option_quotes, option_monitor_now)
        option_exits = self._process_option_exits(option_quotes, option_monitor_now)
        if clock.minutes_to_close is not None and clock.minutes_to_close <= int(self.config["paper"].get("exit_before_close_minutes", 10)):
            portfolio = self._record_portfolio_snapshot(quotes, clock, option_quotes, option_data_error)
            event = {
                "event": "forward_cycle_exit_only",
                "clock": clock.to_dict(),
                "open_order_updates": open_updates,
                "exits": exits,
                "option_open_order_updates": option_open_updates,
                "option_exits": option_exits,
                "resolved_outcomes": resolved_outcomes,
                "option_data_error": option_data_error,
                "quote_provider_effective": effective_quote_provider,
                "cycle_id": cycle_id,
            }
            event["portfolio"] = portfolio
            append_jsonl(self.root, "decisions.jsonl", event)
            write_heartbeat(self.root, "ok", event, now=decision_now)
            return event

        positions = self.broker.store.positions()
        snapshots: dict[str, dict[str, Any]] = {}
        baseline_candidates: list[dict[str, Any]] = []
        active_candidates: list[dict[str, Any]] = []
        expected_completed_session = self.clock.previous_session_date(clock.session)
        upcoming_earnings: dict[str, dict[str, Any]] = {}
        if self.config["paper"].get("strategy_lines", {}).get("options", False):
            try:
                upcoming_earnings = self.option_data.upcoming_earnings(analysis_watchlist, decision_now, days=14)
            except Exception as exc:
                append_jsonl(
                    self.root,
                    "audit.jsonl",
                    {
                        "event": "earnings_calendar_unavailable",
                        "stage": "snapshot_build",
                        "reason": f"{type(exc).__name__}: {exc}",
                        "fail_closed_for_options": True,
                    },
                )
        for symbol in analysis_watchlist:
            quote = quotes.get(symbol)
            if quote is None:
                continue
            event_days = self._days_to_earnings(upcoming_earnings.get(symbol), decision_now)
            snapshot = build_snapshot(
                symbol,
                quote,
                bars,
                clock,
                positions=positions,
                benchmark_quote=quotes.get("SPY"),
                expected_completed_session=expected_completed_session,
                binary_event_within_days=event_days,
            )
            catalyst_signal = self.catalyst_signals.get(symbol, decision_now)
            if catalyst_signal is not None:
                snapshot["market_data"]["catalyst_signal"] = catalyst_signal
            snapshots[symbol] = snapshot
            if symbol not in equity_symbols:
                continue
            baseline = decide_snapshot(snapshot, self.config)
            if baseline["action"] == "buy":
                baseline_candidates.append(baseline)
            active = decide_weighted_snapshot(snapshot, self.config, self.root)
            self.outcome_labeler.register(active, snapshot)
            if active["action"] == "buy":
                active_candidates.append(active)
            append_jsonl(
                self.root,
                "decisions.jsonl",
                {
                    "event": "strategy_comparison",
                    "active_strategy": active,
                    "baseline_shadow": baseline,
                    "snapshot": snapshot,
                },
            )

        max_candidates = int(self.integration_config.get("runtime", {}).get("max_candidates_per_cycle", 3))
        active_candidates.sort(key=lambda item: float(item["score"]), reverse=True)
        selected = active_candidates[:max_candidates]
        shadow_results: list[dict[str, Any]] = []
        orders: list[dict[str, Any]] = []
        # Deterministic execution is intentionally completed before network-bound
        # Exa and LLM shadow research. Shadow latency must not age the quote or
        # alter the active weighted strategy's fill.
        for active in selected:
            symbol = active["ticker"]
            snapshot = snapshots[symbol]
            order = self._submit_weighted_entry(
                snapshot,
                quotes[symbol],
                active,
                quote_adapter=effective_quote_adapter,
                quote_provider=effective_quote_provider,
                cycle_id=cycle_id,
                live_cycle=requested_now is None,
            )
            if order is not None:
                orders.append(order)

        option_snapshots = {
            symbol: snapshot
            for symbol, snapshot in snapshots.items()
            if symbol in option_symbols
        }
        option_entries, option_decisions = self._process_option_entries(option_snapshots, quotes)

        max_shadow_candidates = int(
            self.integration_config.get("runtime", {}).get(
                "max_shadow_candidates_per_cycle",
                1,
            )
        )
        shadow_selected = selected[:max(0, max_shadow_candidates)]
        for active in shadow_selected:
            symbol = active["ticker"]
            enriched = self._attach_news(snapshots[symbol])
            shadow = self.shadow_team.run(enriched).to_dict()
            append_jsonl(self.root, "shadow_decisions.jsonl", shadow)
            shadow_results.append(shadow)

        result = {
            "event": "forward_cycle_complete",
            "cycle_id": cycle_id,
            "clock": clock.to_dict(),
            "quote_provider_effective": effective_quote_provider,
            "quotes": len(quotes),
            "snapshots": len(snapshots),
            "equity_watchlist_count": len(equity_watchlist),
            "option_watchlist_count": len(option_watchlist),
            "baseline_candidates": len(baseline_candidates),
            "active_strategy": "weighted_relative_strength_v2",
            "active_candidates": len(active_candidates),
            "selected_candidates": [item["ticker"] for item in selected],
            "shadow_candidates": [item["ticker"] for item in shadow_selected],
            "open_order_updates": open_updates,
            "exits": exits,
            "option_open_order_updates": option_open_updates,
            "option_exits": option_exits,
            "resolved_outcomes": resolved_outcomes,
            "option_entries": option_entries,
            "option_decisions": option_decisions,
            "option_data_error": option_data_error,
            "orders": orders,
            "shadow_decisions": shadow_results,
            "usage": self.tracker.summary(),
        }
        result["portfolio"] = self._record_portfolio_snapshot(quotes, clock, option_quotes, option_data_error)
        append_jsonl(self.root, "audit.jsonl", result)
        write_heartbeat(self.root, "ok", {"event": result["event"], "orders": len(orders)}, now=decision_now)
        return result

    def _build_quote_adapter(
        self,
        provider: str,
        forward: dict[str, Any],
        integrations: dict[str, Any],
    ) -> Any:
        if provider == "alpaca":
            return AlpacaMarketDataAdapter(forward.get("alpaca", {}))
        if provider == "robinhood_mcp":
            return RobinhoodMcpMarketDataAdapter(
                integrations.get("robinhood_mcp", {}),
                root=self.root,
            )
        raise ValueError(f"unsupported forward quote provider: {provider}")

    def _record_forward_stage(
        self,
        cycle_id: str,
        observed_at: str,
        stage: str,
        status: str,
        **details: Any,
    ) -> None:
        append_jsonl(
            self.root,
            "forward_stages.jsonl",
            {
                "event": "forward_stage",
                "cycle_id": cycle_id,
                "observed_at": observed_at,
                "stage": stage,
                "status": status,
                **details,
            },
        )

    def _fetch_forward_quotes(
        self,
        cycle_id: str,
        observed_at: str,
        symbols: list[str],
        liquidity: dict[str, float | None],
        asset_classes: dict[str, str],
    ) -> tuple[dict[str, Quote], Any, str]:
        attempts: list[tuple[str, Any]] = [(self.quote_provider, self.quote_adapter)]
        if self.fallback_quote_provider and self.fallback_quote_adapter is not None:
            attempts.append((self.fallback_quote_provider, self.fallback_quote_adapter))
        errors: list[str] = []
        for index, (provider, adapter) in enumerate(attempts):
            stage = "primary_quotes" if index == 0 else "fallback_quotes"
            self._record_forward_stage(cycle_id, observed_at, stage, "started", provider=provider)
            try:
                quotes = adapter.fetch_quotes(
                    symbols,
                    liquidity_usd=liquidity,
                    asset_classes=asset_classes,
                )
            except Exception as exc:
                reason = f"{type(exc).__name__}: {exc}"
                errors.append(f"{provider}: {reason}")
                self._record_forward_stage(
                    cycle_id,
                    observed_at,
                    stage,
                    "failed",
                    provider=provider,
                    reason=reason,
                )
                append_jsonl(
                    self.root,
                    "audit.jsonl",
                    {
                        "event": "forward_quote_provider_failed",
                        "cycle_id": cycle_id,
                        "provider": provider,
                        "fallback_available": index + 1 < len(attempts),
                        "reason": reason,
                    },
                )
                continue
            self._record_forward_stage(
                cycle_id,
                observed_at,
                stage,
                "completed",
                provider=provider,
                quotes=len(quotes),
            )
            return quotes, adapter, provider
        raise AdapterError("all forward quote providers failed: " + " | ".join(errors))

    def run_hourly_research(self, now: str | None = None) -> dict[str, Any]:
        now = now or utc_now()
        clock = self.clock.status(now)
        if not clock.is_regular:
            return {"event": "vibe_research_skipped", "reason": f"market session is {clock.market_session}"}
        result = self.swarm.run(
            self.config["universe"].get("default_watchlist", []),
            now,
            "market regime, relative strength, material catalysts, contrary evidence, and data gaps",
        )
        append_jsonl(self.root, "audit.jsonl", {"event": "vibe_research_complete", "research_only": True, "result": result})
        return result

    def run_catalyst_discovery(self, now: str | None = None) -> dict[str, Any]:
        result = self.catalyst_pipeline.run(now)
        append_jsonl(
            self.root,
            "audit.jsonl",
            {
                "event": result.get("event"),
                "strategy": "exa_deepseek_catalyst_v1",
                "cycle_id": result.get("cycle_id"),
                "candidate_count": result.get("candidate_count", 0),
                "decision_count": len(result.get("decisions", [])),
                "paper_orders_created": result.get("paper_orders_created", 0),
                "live_order_tools_called": False,
            },
        )
        return result

    def run_ai_gated_cycle(self, now: str | None = None) -> dict[str, Any]:
        result = self.ai_gated_pipeline.run(now)
        append_jsonl(
            self.root,
            "audit.jsonl",
            {
                "event": result.get("event"),
                "strategy": "ai_gated_technical_v1",
                "cycle_id": result.get("cycle_id"),
                "technical_candidate_count": result.get("technical_candidate_count", 0),
                "decision_count": len(result.get("decisions", [])),
                "paper_orders_created": result.get("paper_orders_created", 0),
                "paper_sleeve": result.get("paper_sleeve"),
                "live_order_tools_called": False,
            },
        )
        return result

    def run_ai_gated_monitor(self, now: str | None = None) -> dict[str, Any]:
        result = self.ai_gated_pipeline.monitor_only(now or utc_now())
        append_jsonl(
            self.root,
            "audit.jsonl",
            {
                "event": result.get("event"),
                "strategy": "ai_gated_technical_v1",
                "paper_sleeve": result.get("paper_sleeve"),
                "monitor_only": True,
                "live_order_tools_called": False,
            },
        )
        return result

    def run_news_drift_cycle(self, now: str | None = None) -> dict[str, Any]:
        return run_news_drift_once(self.root, now)

    def run_eod_guard(self, now: str | None = None) -> dict[str, Any]:
        decision_time = now or utc_now()
        clock = self.clock.status(decision_time)
        equity_positions = self.broker.store.positions()
        option_positions = self.option_broker.store.positions()
        ai_equity_positions = self.ai_gated_pipeline.broker.store.positions()
        ai_option_positions = self.ai_gated_pipeline.option_broker.store.positions()
        if not clock.is_regular:
            return {
                "event": "eod_guard_idle",
                "reason": f"market session is {clock.market_session}",
                "clock": clock.to_dict(),
            }
        overnight = any(
            parse_ts(position.opened_at).date() < parse_ts(clock.open_time or decision_time).date()
            for position in equity_positions.values()
        ) or any(
            parse_ts(position.opened_at).date() < parse_ts(clock.open_time or decision_time).date()
            for position in option_positions.values()
        ) or any(
            parse_ts(position.opened_at).date() < parse_ts(clock.open_time or decision_time).date()
            for position in ai_equity_positions.values()
        ) or any(
            parse_ts(position.opened_at).date() < parse_ts(clock.open_time or decision_time).date()
            for position in ai_option_positions.values()
        )
        preclose = (
            clock.minutes_to_close is not None
            and clock.minutes_to_close <= int(self.config["paper"].get("exit_before_close_minutes", 10))
        )
        if not overnight and not preclose:
            return {"event": "eod_guard_idle", "reason": "outside flatten window", "clock": clock.to_dict()}

        reason = "overnight recovery flatten" if overnight else "mandatory pre-close flatten"
        for order in list(self.broker.store.orders().values()):
            if order.status in {"created", "submitted_to_paper_broker", "open", "partially_filled"}:
                self.broker.cancel_order(order.order_id, reason)
        for order in list(self.option_broker.store.orders().values()):
            if order.status in {"created", "submitted_to_paper_broker", "open", "partially_filled"}:
                self.option_broker.cancel_order(order.order_id, reason)

        quotes = self._fetch_eod_equity_quotes(equity_positions)
        equity_exits: list[dict[str, Any]] = []
        for symbol, position in list(equity_positions.items()):
            quote = quotes.get(symbol)
            if quote is None:
                equity_exits.append({"symbol": symbol, "status": "failed_closed", "reason": "missing EOD quote"})
                continue
            execution_now = max(parse_ts(decision_time), parse_ts(quote.asof)).isoformat()
            order = self.broker.create_order(
                decision_id=f"eod_guard:{clock.session}:{symbol}:{reason}",
                symbol=symbol,
                side="sell",
                order_type="market",
                quantity=position.quantity,
                limit_price=None,
                quote_seen_at=quote.asof,
                thesis=reason,
                idempotency_key=f"eod_guard:{clock.session}:{symbol}:{reason}",
                now=execution_now,
            )
            submitted = self.broker.submit_order(order, quote, execution_now)
            write_order_journal(self.root, submitted, note=reason)
            equity_exits.append({"symbol": symbol, "status": submitted.status, "order": submitted.to_dict()})

        option_exits: list[dict[str, Any]] = []
        try:
            option_quotes = self.option_data.fetch_quotes(list(option_positions))
        except Exception as exc:
            option_quotes = {}
            append_jsonl(
                self.root,
                "audit.jsonl",
                {"event": "eod_option_quote_failed_closed", "reason": f"{type(exc).__name__}: {exc}"},
            )
        for option_id, position in list(option_positions.items()):
            quote = option_quotes.get(option_id)
            if quote is None:
                option_exits.append({"option_id": option_id, "status": "failed_closed", "reason": "missing EOD option quote"})
                continue
            execution_now = max(parse_ts(decision_time), parse_ts(quote.updated_at)).isoformat()
            order = self.option_broker.create_order(
                decision_id=f"eod_guard:{clock.session}:{option_id}:{reason}",
                contract=position.contract,
                intent="sell_to_close",
                order_type="market",
                quantity=position.quantity,
                limit_price=None,
                quote_seen_at=quote.updated_at,
                thesis=reason,
                idempotency_key=f"eod_guard:{clock.session}:{option_id}:{reason}",
                now=execution_now,
            )
            submitted = self.option_broker.submit_order(order, quote, execution_now)
            option_exits.append({"option_id": option_id, "status": submitted.status, "order": submitted.to_dict()})
        ai_gated = self.ai_gated_pipeline.monitor_only(decision_time, force_flatten=True)
        current_account = self.broker.store.account()
        remaining_equity_positions = self.broker.store.positions()
        remaining_option_positions = self.option_broker.store.positions()
        conservative_equity = (
            current_account.cash
            + sum(
                position.average_price * position.quantity
                for position in remaining_equity_positions.values()
            )
            + sum(position.cost_basis() for position in remaining_option_positions.values())
        )
        result = {
            "event": "eod_guard_complete",
            "reason": reason,
            "clock": clock.to_dict(),
            "equity_exits": equity_exits,
            "option_exits": option_exits,
            "ai_gated": ai_gated,
            "main_account_valuation": {
                "cash": round(current_account.cash, 4),
                "realized_pnl": round(current_account.realized_pnl, 4),
                "conservative_equity": round(conservative_equity, 4),
                "open_equity_positions": len(remaining_equity_positions),
                "open_option_positions": len(remaining_option_positions),
                "asof": current_account.updated_at,
            },
            "live_order_tools_called": False,
        }
        append_jsonl(self.root, "audit.jsonl", result)
        return result

    def _fetch_eod_equity_quotes(self, positions: dict[str, Any]) -> dict[str, Quote]:
        symbols = list(positions)
        if not symbols:
            return {}
        liquidity = {symbol: None for symbol in symbols}
        etf_symbols = {
            str(symbol).upper()
            for symbol in self.config["universe"].get("etf_symbols", [])
        }
        asset_classes = {
            symbol: "us_etf" if symbol in etf_symbols else "us_equity"
            for symbol in symbols
        }
        alpaca_config = self.integration_config.get("forward_data", {}).get("alpaca", {})
        alpaca = AlpacaMarketDataAdapter(alpaca_config)
        if alpaca.readiness().get("ready"):
            try:
                return alpaca.fetch_quotes(symbols, liquidity_usd=liquidity, asset_classes=asset_classes)
            except Exception as exc:
                append_jsonl(
                    self.root,
                    "audit.jsonl",
                    {"event": "eod_alpaca_quote_fallback", "reason": f"{type(exc).__name__}: {exc}"},
                )
        try:
            return self.quote_adapter.fetch_quotes(symbols, liquidity_usd=liquidity, asset_classes=asset_classes)
        except Exception as exc:
            append_jsonl(
                self.root,
                "audit.jsonl",
                {"event": "eod_equity_quote_failed_closed", "reason": f"{type(exc).__name__}: {exc}"},
            )
            return {}

    def _attach_news(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        enriched = dict(snapshot)
        try:
            news, metadata = self.news_adapter.search(snapshot["ticker"], snapshot["data_cutoff_time"])
        except AdapterError as exc:
            news = []
            metadata = [{"source": "exa", "source_tier": 4, "error": str(exc)}]
        enriched["available_news"] = news
        enriched["source_metadata"] = [*snapshot["source_metadata"], *metadata]
        observed_times = [snapshot["decision_time"]]
        observed_times.extend(
            str(item.get("retrieved_at") or item.get("first_seen_at"))
            for item in news
            if item.get("retrieved_at") or item.get("first_seen_at")
        )
        observed_times.extend(
            str(item.get("retrieved_at"))
            for item in metadata
            if item.get("retrieved_at")
        )
        refreshed_cutoff = max(parse_ts(value) for value in observed_times).isoformat()
        enriched["decision_time"] = refreshed_cutoff
        enriched["data_cutoff_time"] = refreshed_cutoff
        return enriched

    def _submit_weighted_entry(
        self,
        snapshot: dict[str, Any],
        quote: Quote,
        decision: dict[str, Any],
        *,
        quote_adapter: Any | None = None,
        quote_provider: str | None = None,
        cycle_id: str | None = None,
        live_cycle: bool = False,
    ) -> dict[str, Any] | None:
        account = self.broker.store.account()
        positions = self.broker.store.positions()
        if snapshot["ticker"] in positions:
            return None
        adapter = quote_adapter or self.quote_adapter
        provider = quote_provider or self.quote_provider
        try:
            if cycle_id:
                self._record_forward_stage(
                    cycle_id,
                    snapshot["decision_time"],
                    "entry_quote_refresh",
                    "started",
                    provider=provider,
                    ticker=snapshot["ticker"],
                )
            refreshed = adapter.fetch_quotes(
                [snapshot["ticker"]],
                liquidity_usd={snapshot["ticker"]: quote.avg_daily_volume_usd},
                asset_classes={snapshot["ticker"]: quote.asset_class},
            ).get(snapshot["ticker"])
            if cycle_id:
                self._record_forward_stage(
                    cycle_id,
                    snapshot["decision_time"],
                    "entry_quote_refresh",
                    "completed",
                    provider=provider,
                    ticker=snapshot["ticker"],
                )
        except Exception as exc:
            refreshed = None
            if cycle_id:
                self._record_forward_stage(
                    cycle_id,
                    snapshot["decision_time"],
                    "entry_quote_refresh",
                    "failed",
                    provider=provider,
                    ticker=snapshot["ticker"],
                    reason=f"{type(exc).__name__}: {exc}",
                )
            append_jsonl(
                self.root,
                "audit.jsonl",
                {
                    "event": "entry_quote_refresh_failed_closed",
                    "ticker": snapshot["ticker"],
                    "reason": f"{type(exc).__name__}: {exc}",
                },
            )
        if refreshed is None:
            return None
        buffer = float(self.integration_config.get("runtime", {}).get("order_notional_buffer_pct", 0.96))
        quantity = calculate_entry_quantity(account, positions, refreshed, self.config["risk"], notional_buffer_pct=buffer)
        if quantity <= 0:
            append_jsonl(self.root, "decisions.jsonl", {"event": "weighted_order_skipped", "ticker": snapshot["ticker"], "reason": "paper account has no capacity within position and shared caps"})
            return None
        limit_price = self._equity_buy_limit(refreshed.ask)
        desired_notional = float(quantity) * limit_price
        capacity = shared_entry_capacity(
            line="equity",
            account=account,
            equity_positions=positions,
            option_positions=self.option_broker.store.positions(),
            equity_orders=self.broker.store.orders(),
            option_orders=self.option_broker.store.orders(),
            shared_config=self.config.get("shared_risk", {}),
        )
        if desired_notional > capacity + 1e-9:
            append_jsonl(
                self.root,
                "decisions.jsonl",
                {
                    "event": "weighted_order_skipped",
                    "ticker": snapshot["ticker"],
                    "reason": "shared paper account has insufficient entry capacity",
                    "desired_notional_usd": round(desired_notional, 4),
                    "available_capacity_usd": round(capacity, 4),
                },
            )
            return None
        execution_observed_at = utc_now() if live_cycle else snapshot["decision_time"]
        execution_now = max(
            parse_ts(snapshot["decision_time"]),
            parse_ts(refreshed.asof),
            parse_ts(execution_observed_at),
        ).isoformat()
        order = self.broker.create_order(
            decision_id=snapshot["snapshot_id"],
            symbol=snapshot["ticker"],
            side="buy",
            order_type="limit",
            quantity=float(quantity),
            limit_price=limit_price,
            quote_seen_at=refreshed.asof,
            thesis=f"weighted_relative_strength_v2 score={float(decision['score']):.4f}",
            idempotency_key=f"weighted_relative_strength_v2:{snapshot['snapshot_id']}",
            now=execution_now,
        )
        submitted = self.broker.submit_order(order, refreshed, now=execution_now)
        journal = write_order_journal(self.root, submitted, note="forward weighted paper entry")
        return {
            "strategy": "weighted_relative_strength_v2",
            "decision": decision,
            "order": submitted.to_dict(),
            "journal_path": str(journal),
        }

    def _equity_buy_limit(self, ask: float) -> float:
        costs = self.config.get("costs", {})
        slip = max(
            ask * float(costs.get("slippage_bps", 0)) / 10000,
            float(costs.get("minimum_slippage_usd", 0)),
        )
        buffer_bps = float(self.integration_config.get("runtime", {}).get("aggressive_limit_buffer_bps", 0))
        return round(ask + max(slip, ask * buffer_bps / 10000), 4)

    def _process_exits(self, quotes: dict[str, Quote], clock: Any) -> list[dict[str, Any]]:
        exits: list[dict[str, Any]] = []
        for symbol, position in list(self.broker.store.positions().items()):
            quote = quotes.get(symbol)
            decision = evaluate_position_exit(
                position,
                quote,
                clock.asof,
                self.config["risk"],
                minutes_to_close=clock.minutes_to_close,
                exit_before_close_minutes=int(self.config["paper"].get("exit_before_close_minutes", 10)),
            )
            append_jsonl(self.root, "decisions.jsonl", {"event": "exit_evaluation", "symbol": symbol, "decision": decision.to_dict()})
            if not decision.should_exit or quote is None:
                continue
            order = self.broker.create_order(
                decision_id=f"exit:{symbol}:{clock.session}:{decision.reason}",
                symbol=symbol,
                side="sell",
                order_type="market",
                quantity=position.quantity,
                limit_price=None,
                quote_seen_at=quote.asof,
                thesis=decision.reason,
                idempotency_key=f"exit:{symbol}:{clock.session}:{decision.reason}",
                now=clock.asof,
            )
            submitted = self.broker.submit_order(order, quote, now=clock.asof)
            journal = write_order_journal(self.root, submitted, note=f"forward paper exit: {decision.reason}")
            exits.append({"decision": decision.to_dict(), "order": submitted.to_dict(), "journal_path": str(journal)})
        return exits

    def _fetch_held_option_quotes(self) -> tuple[dict[str, Any], str | None]:
        option_ids = {position.contract.option_id for position in self.option_broker.store.positions().values()}
        option_ids.update(
            order.contract.option_id
            for order in self.option_broker.store.orders().values()
            if order.status in {"created", "submitted_to_paper_broker", "open", "partially_filled"}
        )
        if not option_ids:
            return {}, None
        try:
            return self.option_data.fetch_quotes(sorted(option_ids)), None
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            append_jsonl(self.root, "audit.jsonl", {"event": "option_monitor_failed_closed", "reason": message, "option_ids": sorted(option_ids)})
            return {}, message

    def _process_option_exits(self, option_quotes: dict[str, Any], now: str) -> list[dict[str, Any]]:
        exits: list[dict[str, Any]] = []
        for option_id, position in list(self.option_broker.store.positions().items()):
            quote = option_quotes.get(option_id)
            decision = evaluate_option_exit(position, quote, now, self.config["options_risk"])
            append_jsonl(
                self.root,
                "decisions.jsonl",
                {"event": "option_exit_evaluation", "option_id": option_id, "contract": position.contract.to_dict(), "decision": decision.to_dict()},
            )
            if not decision.should_exit or quote is None:
                continue
            order = self.option_broker.create_order(
                decision_id=f"option_exit:{option_id}:{now[:10]}:{decision.reason}",
                contract=position.contract,
                intent="sell_to_close",
                order_type="market",
                quantity=position.quantity,
                limit_price=None,
                quote_seen_at=quote.updated_at,
                thesis=decision.reason,
                idempotency_key=f"option_exit:{option_id}:{now[:10]}:{decision.reason}",
                now=now,
            )
            submitted = self.option_broker.submit_order(order, quote, now)
            append_jsonl(self.root, "option_journal.jsonl", {"event": "option_exit", "decision": decision.to_dict(), "order": submitted.to_dict()})
            exits.append({"decision": decision.to_dict(), "order": submitted.to_dict()})
        return exits

    def _process_option_entries(self, snapshots: dict[str, dict[str, Any]], quotes: dict[str, Quote]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if not self.config["paper"].get("strategy_lines", {}).get("options", False):
            return [], []
        baseline_decisions = [decide_option_direction(snapshot, self.config) for snapshot in snapshots.values()]
        decisions = [decide_weighted_option_direction(snapshot, self.config) for snapshot in snapshots.values()]
        candidates = [item for item in decisions if item["action"] == "buy_to_open"]
        candidates.sort(key=lambda item: float(item.get("score", 0)), reverse=True)
        max_candidates = int(self.integration_config.get("runtime", {}).get("max_option_candidates_per_cycle", 1))
        candidates = candidates[:max_candidates]
        if len(self.option_broker.store.positions()) >= int(self.config["options_risk"].get("max_open_positions", 1)):
            for candidate in candidates:
                candidate["action"] = "no_trade"
                candidate["reasons"].append("max open option positions already reached")
            candidates = []
        elif any(
            order.status in {"created", "submitted_to_paper_broker", "open", "partially_filled"}
            for order in self.option_broker.store.orders().values()
        ):
            for candidate in candidates:
                candidate["action"] = "no_trade"
                candidate["reasons"].append("open option order already pending")
            candidates = []
        if candidates:
            try:
                earnings = self.option_data.upcoming_earnings([item["ticker"] for item in candidates], utc_now(), days=7)
            except Exception as exc:
                earnings = None
                append_jsonl(self.root, "audit.jsonl", {"event": "option_entry_failed_closed", "stage": "earnings_calendar", "reason": f"{type(exc).__name__}: {exc}"})
            if earnings is None:
                candidates = []
            else:
                for item in candidates:
                    if item["ticker"] in earnings:
                        item["action"] = "no_trade"
                        item["reasons"].append("upcoming earnings found by Robinhood calendar")
                candidates = [item for item in candidates if item["action"] == "buy_to_open"]

        entries: list[dict[str, Any]] = []
        for candidate in candidates:
            option_positions = self.option_broker.store.positions()
            account = self.option_broker.store.base.account()
            equity_positions = self.option_broker.store.base.positions()
            account_equity_at_cost = account.cash
            account_equity_at_cost += sum(position.average_price * position.quantity for position in equity_positions.values())
            account_equity_at_cost += sum(position.cost_basis() for position in option_positions.values())
            premium_budget = account_equity_at_cost * float(self.config["options_risk"].get("max_order_risk_pct_of_equity", 0.10)) * 0.95
            symbol = candidate["ticker"]
            try:
                selection_time = utc_now()
                if hasattr(self.option_data, "fetch_best_contract_with_diagnostics"):
                    selected, diagnostics = self.option_data.fetch_best_contract_with_diagnostics(
                        underlying=symbol,
                        underlying_price=quotes[symbol].last,
                        option_type=candidate["option_type"],
                        now=selection_time,
                        max_premium_usd=premium_budget,
                    )
                else:
                    selected = self.option_data.fetch_best_contract(
                        underlying=symbol,
                        underlying_price=quotes[symbol].last,
                        option_type=candidate["option_type"],
                        now=selection_time,
                        max_premium_usd=premium_budget,
                    )
                    diagnostics = {"diagnostics_unavailable": True}
                candidate["contract_selection_diagnostics"] = diagnostics
                append_jsonl(
                    self.root,
                    "option_selection_diagnostics.jsonl",
                    {"ticker": symbol, "decision": candidate, "diagnostics": diagnostics},
                )
            except Exception as exc:
                candidate["action"] = "no_trade"
                candidate["reasons"].append(f"option data failed closed: {type(exc).__name__}")
                append_jsonl(self.root, "audit.jsonl", {"event": "option_entry_failed_closed", "stage": "contract_selection", "ticker": symbol, "reason": f"{type(exc).__name__}: {exc}"})
                continue
            if selected is None:
                candidate["action"] = "no_trade"
                candidate["reasons"].append(
                    f"no contract passed filters: {diagnostics.get('rejections', {})}"
                )
                continue
            contract, option_quote = selected
            execution_now = utc_now(timespec="microseconds")
            order = self.option_broker.create_order(
                decision_id=f"{candidate['snapshot_id']}:options",
                contract=contract,
                intent="buy_to_open",
                order_type="limit",
                quantity=1,
                limit_price=self._option_buy_limit(option_quote.ask, contract),
                quote_seen_at=option_quote.updated_at,
                thesis=f"long_directional_options_v2_weighted score={float(candidate['score']):.4f}",
                idempotency_key=f"long_directional_options_v2_weighted:{candidate['snapshot_id']}:{contract.option_id}",
                now=execution_now,
            )
            submitted = self.option_broker.submit_order(order, option_quote, execution_now)
            record = {"event": "option_entry", "decision": candidate, "contract": contract.to_dict(), "quote": option_quote.to_dict(), "order": submitted.to_dict()}
            append_jsonl(self.root, "option_journal.jsonl", record)
            entries.append(record)
            if submitted.status == "filled":
                break
        for decision in decisions:
            append_jsonl(self.root, "decisions.jsonl", {"event": "option_strategy_decision", "decision": decision, "snapshot_id": decision["snapshot_id"]})
        for decision in baseline_decisions:
            append_jsonl(
                self.root,
                "decisions.jsonl",
                {
                    "event": "option_baseline_shadow_decision",
                    "decision": decision,
                    "snapshot_id": decision["snapshot_id"],
                },
            )
        return entries, decisions

    def _option_buy_limit(self, ask: float, contract: Any) -> float:
        costs = self.config.get("options_costs", {})
        slip = max(
            ask * float(costs.get("slippage_bps", 0)) / 10000,
            float(costs.get("minimum_slippage_usd_per_contract", 0)),
        )
        tick_reference = ask + slip
        tick = (
            contract.above_tick
            if tick_reference > contract.tick_cutoff_price
            else contract.below_tick
        ) or float(costs.get("price_tick_usd", 0.01))
        units = int((tick_reference + tick - 1e-12) / tick)
        return round(units * tick, 4)

    def _record_portfolio_snapshot(
        self,
        quotes: dict[str, Quote],
        clock: Any,
        observed_option_quotes: dict[str, Any] | None = None,
        observed_option_error: str | None = None,
    ) -> dict[str, Any]:
        account = self.broker.store.account()
        positions = self.broker.store.positions()
        missing_quotes = sorted(symbol for symbol in positions if symbol not in quotes)
        option_positions = self.option_broker.store.positions()
        held_option_quotes = dict(observed_option_quotes or {})
        missing_observations = [option_id for option_id in option_positions if option_id not in held_option_quotes]
        option_quote_error = observed_option_error
        if missing_observations:
            try:
                held_option_quotes.update(self.option_data.fetch_quotes(missing_observations))
            except Exception as exc:
                option_quote_error = f"{type(exc).__name__}: {exc}"
        missing_option_quotes = sorted(option_id for option_id in option_positions if option_id not in held_option_quotes)
        equity = None if missing_quotes or missing_option_quotes else account.equity(positions, quotes) + sum(
            position.liquidation_value(held_option_quotes.get(option_id)) for option_id, position in option_positions.items()
        )
        unrealized = None
        if equity is not None:
            equity_cost = sum(position.average_price * position.quantity for position in positions.values())
            option_cost = sum(position.cost_basis() for position in option_positions.values())
            equity_market_value = sum(position.market_value(quotes.get(symbol)) for symbol, position in positions.items())
            option_market_value = sum(
                position.liquidation_value(held_option_quotes.get(option_id)) for option_id, position in option_positions.items()
            )
            equity_unrealized = equity_market_value - equity_cost
            option_unrealized = option_market_value - option_cost
            unrealized = equity_unrealized + option_unrealized
        else:
            equity_market_value = None
            option_market_value = None
            equity_unrealized = None
            option_unrealized = None
        snapshot = {
            "event": "portfolio_snapshot",
            "session": clock.session,
            "asof": clock.asof,
            "cash": round(account.cash, 4),
            "equity": round(equity, 4) if equity is not None else None,
            "realized_pnl": round(account.realized_pnl, 4),
            "unrealized_pnl": round(unrealized, 4) if unrealized is not None else None,
            "equity_market_value": round(equity_market_value, 4) if equity_market_value is not None else None,
            "option_market_value": round(option_market_value, 4) if option_market_value is not None else None,
            "equity_unrealized_pnl": round(equity_unrealized, 4) if equity_unrealized is not None else None,
            "option_unrealized_pnl": round(option_unrealized, 4) if option_unrealized is not None else None,
            "positions": {symbol: position.to_dict() for symbol, position in positions.items()},
            "option_positions": {option_id: position.to_dict() for option_id, position in option_positions.items()},
            "missing_position_quotes": missing_quotes,
            "missing_option_position_quotes": missing_option_quotes,
            "option_quote_error": option_quote_error,
            "option_greeks": aggregate_portfolio_greeks(option_positions, held_option_quotes),
        }
        append_jsonl(self.root, "portfolio_snapshots.jsonl", snapshot)
        return snapshot

    @staticmethod
    def _days_to_earnings(item: dict[str, Any] | None, now: str) -> int:
        if not isinstance(item, dict):
            return 99
        report = item.get("report")
        values: list[Any] = []
        if isinstance(report, dict):
            values.extend(
                report.get(key)
                for key in ("date", "report_date", "begins_at", "time")
            )
        else:
            values.append(report)
        values.extend(item.get(key) for key in ("date", "report_date", "begins_at"))
        for value in values:
            if not value:
                continue
            try:
                return max(0, (parse_ts(str(value)).date() - parse_ts(now).date()).days)
            except ValueError:
                try:
                    return max(0, (datetime.fromisoformat(str(value)).date() - parse_ts(now).date()).days)
                except ValueError:
                    continue
        return 0


def _cycle_summary(result: dict[str, Any]) -> dict[str, Any]:
    """Return a compact, non-sensitive console status for one runtime event."""
    clock = result.get("clock", {})
    summary: dict[str, Any] = {
        "event": result.get("event"),
        "asof": clock.get("asof"),
        "market_session": clock.get("market_session"),
    }
    for name in ("quotes", "snapshots", "baseline_candidates"):
        if name in result:
            summary[name] = result[name]
    if "selected_candidates" in result:
        summary["selected_candidates"] = result["selected_candidates"]
    if "shadow_decisions" in result:
        summary["shadow_decisions"] = len(result["shadow_decisions"])
    if "orders" in result:
        summary["paper_orders"] = len(result["orders"])
    if "exits" in result:
        summary["paper_exits"] = len(result["exits"])
    if "option_entries" in result:
        summary["paper_option_entries"] = len(result["option_entries"])
    if "option_exits" in result:
        summary["paper_option_exits"] = len(result["option_exits"])
    if "reason" in result:
        summary["reason"] = result["reason"]
    if "stage" in result:
        summary["stage"] = result["stage"]
    return summary


def _emit_runtime_event(event: dict[str, Any]) -> None:
    print(json.dumps(event, sort_keys=True), flush=True)


def serve(root: str | Path) -> None:
    root = Path(root).resolve()
    config = load_runtime_config(root)
    assert_paper_mode(config)
    runtime = config.get("integrations", {}).get("runtime", {})
    scheduler = BlockingScheduler(timezone="America/New_York")
    runner = SubprocessJobRunner(root)
    latest_results: dict[str, dict[str, Any]] = {}

    def run_worker(
        job_name: str,
        args: list[str],
        timeout_key: str,
        default_timeout: int,
        *,
        resources: set[str],
    ) -> None:
        result = runner.run(
            job_name,
            args,
            timeout_seconds=float(runtime.get(timeout_key, default_timeout)),
            mutates_state=bool(resources),
            resources=resources,
        )
        latest_results[job_name] = result.to_dict()
        output = result.output or {}
        _emit_runtime_event(
            {
                "event": "supervised_worker_result",
                "job": job_name,
                "status": result.status,
                "elapsed_seconds": result.elapsed_seconds,
                "child_event": output.get("event"),
                "paper_orders_created": output.get("paper_orders_created"),
                "reason": result.error or output.get("reason"),
            }
        )

    def run_forward_cycle() -> None:
        run_worker(
            "forward",
            ["--once"],
            "forward_worker_timeout_seconds",
            300,
            resources={"main_account"},
        )

    def run_research_cycle() -> None:
        run_worker(
            "research",
            ["--research-once"],
            "research_worker_timeout_seconds",
            300,
            resources=set(),
        )

    def run_catalyst_cycle() -> None:
        run_worker(
            "catalyst",
            ["--catalyst-once"],
            "catalyst_worker_timeout_seconds",
            600,
            resources={"main_account", "evidence_store"},
        )

    def run_ai_gated_cycle() -> None:
        run_worker(
            "ai_gated",
            ["--ai-gated-once"],
            "ai_gated_worker_timeout_seconds",
            420,
            resources={"ai_account", "evidence_store"},
        )

    def run_ai_monitor() -> None:
        run_worker(
            "ai_monitor",
            ["--ai-monitor-once"],
            "ai_monitor_worker_timeout_seconds",
            90,
            resources={"ai_account"},
        )

    def run_news_drift_cycle() -> None:
        run_worker(
            "news_drift",
            ["--news-drift-once"],
            "news_drift_worker_timeout_seconds",
            120,
            resources={"news_event_store"},
        )

    def run_eod_guard() -> None:
        run_worker(
            "eod_guard",
            ["--eod-once"],
            "eod_worker_timeout_seconds",
            90,
            resources={"main_account", "ai_account"},
        )

    def run_evaluation() -> None:
        run_worker(
            "evaluation",
            ["--evaluate-once"],
            "evaluation_worker_timeout_seconds",
            120,
            resources={"news_event_store"},
        )

    def supervisor_heartbeat() -> None:
        failed = [
            value
            for value in latest_results.values()
            if value.get("status") in {"failed", "timed_out"}
        ]
        status = "degraded" if failed else "ok"
        write_heartbeat(
            root,
            status,
            {
                "event": "forward_service_heartbeat",
                "active_jobs": runner.active_jobs(),
                "latest_jobs": latest_results,
                "paper_mode": True,
                "live_trading": False,
            },
        )

    now = datetime.now(timezone.utc)
    scheduler.add_job(
        run_forward_cycle,
        "interval",
        seconds=int(runtime.get("forward_cycle_seconds", 300)),
        id="forward-paper-cycle",
        max_instances=1,
        coalesce=True,
        next_run_time=now,
    )
    if config.get("integrations", {}).get("vibe", {}).get("research_swarm", {}).get("enabled", False):
        scheduler.add_job(run_research_cycle, "interval", seconds=int(runtime.get("research_cycle_seconds", 3600)), id="vibe-hourly-research", max_instances=1, coalesce=True)
    catalyst_profile = config.get("strategies", {}).get("exa_deepseek_catalyst_v1", {})
    catalyst_discovery = catalyst_profile.get("discovery", {})
    if catalyst_discovery.get("enabled", False):
        scheduler.add_job(
            run_catalyst_cycle,
            "interval",
            seconds=int(catalyst_discovery.get("cycle_seconds", 3600)),
            id="exa-deepseek-catalyst-discovery",
            max_instances=1,
            coalesce=True,
            next_run_time=now
            + timedelta(
                seconds=int(
                    runtime.get("catalyst_start_offset_seconds", 1210)
                )
            ),
        )
    ai_profile = config.get("strategies", {}).get("ai_gated_technical_v1", {})
    if ai_profile.get("enabled", False):
        scheduler.add_job(
            run_ai_gated_cycle,
            "interval",
            seconds=int(ai_profile.get("cycle_seconds", 3600)),
            id="ai-gated-paper-cycle",
            max_instances=1,
            coalesce=True,
            next_run_time=now
            + timedelta(
                seconds=int(runtime.get("ai_gated_start_offset_seconds", 10))
            ),
        )
        scheduler.add_job(
            run_ai_gated_cycle,
            "cron",
            day_of_week="mon-fri",
            hour=9,
            minute=32,
            timezone="America/New_York",
            id="ai-gated-open-revalidation",
            max_instances=1,
            coalesce=True,
        )
        scheduler.add_job(
            run_ai_monitor,
            "interval",
            seconds=int(runtime.get("ai_monitor_cycle_seconds", 300)),
            id="ai-gated-position-monitor",
            max_instances=1,
            coalesce=True,
            next_run_time=now + timedelta(seconds=60),
        )
    news_drift_profile = config.get("strategies", {}).get("llm_news_drift_v1", {})
    if news_drift_profile.get("enabled", False):
        scheduler.add_job(
            run_news_drift_cycle,
            "interval",
            seconds=int(news_drift_profile.get("cycle_seconds", 60)),
            id="llm-news-drift-shadow-cycle",
            max_instances=1,
            coalesce=True,
            next_run_time=now
            + timedelta(seconds=int(runtime.get("news_drift_start_offset_seconds", 20))),
        )
    scheduler.add_job(
        run_eod_guard,
        "interval",
        seconds=int(runtime.get("eod_guard_seconds", 60)),
        id="eod-guard",
        max_instances=1,
        coalesce=True,
        next_run_time=now + timedelta(seconds=30),
    )
    scheduler.add_job(
        supervisor_heartbeat,
        "interval",
        seconds=int(runtime.get("heartbeat_seconds", 30)),
        id="supervisor-heartbeat",
        max_instances=1,
        coalesce=True,
        next_run_time=now,
    )
    scheduler.add_job(
        run_evaluation,
        "interval",
        seconds=int(runtime.get("evaluation_cycle_seconds", 1800)),
        id="paper-performance-evaluation",
        max_instances=1,
        coalesce=True,
        next_run_time=now + timedelta(seconds=40),
    )
    lock = ProcessLock(root / "state" / "forward_service.lock")
    if not lock.acquire():
        append_jsonl(
            root,
            "runtime_service.jsonl",
            {
                "event": "forward_service_start_rejected",
                "reason": "service lock already held",
                "paper_mode": True,
                "live_trading": False,
            },
        )
        raise RuntimeError("forward paper service is already running")

    shutdown_requested = False

    def request_shutdown(signum: int, _frame: Any) -> None:
        nonlocal shutdown_requested
        if shutdown_requested:
            return
        shutdown_requested = True
        append_jsonl(
            root,
            "runtime_service.jsonl",
            {
                "event": "forward_service_stop_requested",
                "signal": signum,
                "pid": os.getpid(),
            },
        )
        _emit_runtime_event({"event": "forward_service_stop_requested", "signal": signum})
        if scheduler.running:
            scheduler.shutdown(wait=False)
        runner.terminate_all()

    previous_handlers: dict[int, Any] = {}
    try:
        handled_signals = [signal.SIGINT, signal.SIGTERM]
        if hasattr(signal, "SIGBREAK"):
            handled_signals.append(signal.SIGBREAK)
        for signum in handled_signals:
            previous_handlers[signum] = signal.signal(signum, request_shutdown)
        append_jsonl(
            root,
            "runtime_service.jsonl",
            {
                "event": "forward_service_started",
                "pid": os.getpid(),
                "paper_mode": True,
                "live_trading": False,
            },
        )
        write_heartbeat(root, "idle", {"event": "forward_service_started"})
        _emit_runtime_event(
            {
                "event": "forward_service_started",
                "cycle_seconds": int(runtime.get("forward_cycle_seconds", 300)),
                "paper_mode": True,
                "live_trading": False,
            }
        )
        scheduler.start()
    except KeyboardInterrupt:
        shutdown_requested = True
        append_jsonl(
            root,
            "runtime_service.jsonl",
            {
                "event": "forward_service_stop_requested",
                "signal": "KeyboardInterrupt",
                "pid": os.getpid(),
            },
        )
        _emit_runtime_event({"event": "forward_service_stop_requested", "signal": "KeyboardInterrupt"})
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)
        runner.terminate_all()
        for signum, previous in previous_handlers.items():
            signal.signal(signum, previous)
        append_jsonl(
            root,
            "runtime_service.jsonl",
            {
                "event": "forward_service_stopped",
                "pid": os.getpid(),
                "shutdown_requested": shutdown_requested,
            },
        )
        write_heartbeat(
            root,
            "stopped",
            {
                "event": "forward_service_stopped",
                "shutdown_requested": shutdown_requested,
            },
        )
        lock.release()
        _emit_runtime_event({"event": "forward_service_stopped"})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--readiness", action="store_true")
    parser.add_argument("--catalyst-once", action="store_true")
    parser.add_argument("--ai-gated-once", action="store_true")
    parser.add_argument("--ai-monitor-once", action="store_true")
    parser.add_argument("--news-drift-once", action="store_true")
    parser.add_argument("--research-once", action="store_true")
    parser.add_argument("--eod-once", action="store_true")
    parser.add_argument("--evaluate-once", action="store_true")
    parser.add_argument("--now")
    args = parser.parse_args()
    if not any(
        (
            args.readiness,
            args.catalyst_once,
            args.ai_gated_once,
            args.ai_monitor_once,
            args.news_drift_once,
            args.research_once,
            args.eod_once,
            args.evaluate_once,
            args.once,
        )
    ):
        serve(args.root)
        return
    if args.evaluate_once:
        report_path = generate_report(args.root)
        news_drift_report_path = generate_news_drift_report(args.root)
        result = {
            "event": "paper_evaluation_complete",
            "metrics": calculate_metrics(args.root),
            "report_path": str(report_path),
            "news_drift_metrics": calculate_news_drift_metrics(args.root),
            "news_drift_report_path": str(news_drift_report_path),
            "live_order_tools_called": False,
        }
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    mutates_state = any(
        (
            args.catalyst_once,
            args.ai_gated_once,
            args.ai_monitor_once,
            args.news_drift_once,
            args.eod_once,
            args.once,
        )
    )
    command_lock: ProcessLock | None = None
    if mutates_state and os.getenv("AUTO_TRADING_SUPERVISED_CHILD") != "1":
        command_lock = ProcessLock(Path(args.root).resolve() / "state" / "forward_service.lock")
        if not command_lock.acquire():
            raise RuntimeError(
                "forward paper service is running; stop it before running a standalone state-mutating cycle"
            )
    try:
        if args.news_drift_once:
            result = run_news_drift_once(args.root, args.now)
        else:
            service = ForwardPaperService(args.root)
            if args.readiness:
                result = service.readiness()
            elif args.catalyst_once:
                result = service.run_catalyst_discovery(args.now)
            elif args.ai_gated_once:
                result = service.run_ai_gated_cycle(args.now)
            elif args.ai_monitor_once:
                result = service.run_ai_gated_monitor(args.now)
            elif args.research_once:
                result = service.run_hourly_research(args.now)
            elif args.eod_once:
                result = service.run_eod_guard(args.now)
            elif args.once:
                result = service.run_once(args.now)
        print(json.dumps(result, indent=2, sort_keys=True))
    finally:
        if command_lock is not None:
            command_lock.release()


if __name__ == "__main__":
    main()
