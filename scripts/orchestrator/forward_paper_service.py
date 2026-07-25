from __future__ import annotations

import argparse
import json
import time
import os
import signal
from datetime import datetime, timezone
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
from scripts.exit.evaluate_exit import evaluate_position_exit
from scripts.journal.write_trade_journal import write_order_journal
from scripts.llm import build_provider
from scripts.research.snapshot_builder import build_snapshot
from scripts.risk.position_sizing import calculate_entry_quantity
from scripts.runtime.heartbeat import write_heartbeat
from scripts.runtime.market_clock import UsEquityMarketClock
from scripts.runtime.process_lock import ProcessLock
from scripts.options.exit_policy import evaluate_option_exit
from scripts.options.paper_broker import OptionPaperBroker
from scripts.options.portfolio import aggregate_portfolio_greeks
from scripts.options.strategy import decide_option_direction
from scripts.simulation.paper_broker import PaperBroker
from scripts.strategies.relative_strength_v1 import decide_snapshot


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
        if self.quote_provider == "alpaca":
            self.quote_adapter = AlpacaMarketDataAdapter(forward.get("alpaca", {}))
        elif self.quote_provider == "robinhood_mcp":
            self.quote_adapter = RobinhoodMcpMarketDataAdapter(integrations.get("robinhood_mcp", {}), root=self.root)
        else:
            raise ValueError(f"unsupported forward quote provider: {self.quote_provider}")
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
        self.tracker = tracker

    def readiness(self) -> dict[str, Any]:
        vibe_status = self.vibe.runtime.status().to_dict()
        llm_provider = str(self.config["llm"].get("provider", "mock"))
        llm_key_env = str(self.config["llm"].get("api", {}).get("api_key_env", "OPENAI_API_KEY"))
        llm_ready = llm_provider == "mock" or bool(os.getenv(llm_key_env))
        quote_provider = self.quote_adapter.readiness()
        option_data = self.option_data.readiness()
        exa = self.news_adapter.readiness()
        return {
            "paper_mode": True,
            "live_trading": False,
            "vibe": vibe_status,
            "quote_provider": self.quote_provider,
            "quote_data": quote_provider,
            "option_data": option_data,
            "exa": exa,
            "llm_provider": llm_provider,
            "llm_api_key_env": llm_key_env if llm_provider == "api" else None,
            "llm_ready": llm_ready,
            "ready_for_forward_quotes": vibe_status["ready"] and quote_provider["ready"],
            "ready_for_news_shadow": exa["ready"] and llm_ready,
            "ready_for_catalyst_discovery": quote_provider["ready"] and exa["ready"] and llm_ready,
            "ready_for_full_forward_evaluation": vibe_status["ready"] and quote_provider["ready"] and option_data["ready"] and exa["ready"] and llm_ready,
        }

    def run_once(self, now: str | None = None) -> dict[str, Any]:
        requested_now = now
        cycle_now = requested_now or utc_now()
        clock = self.clock.status(cycle_now)
        if not clock.is_regular:
            event = {"event": "forward_cycle_skipped", "reason": f"market session is {clock.market_session}", "clock": clock.to_dict()}
            append_jsonl(self.root, "audit.jsonl", event)
            write_heartbeat(self.root, "idle", event, now=cycle_now)
            return event

        watchlist = list(dict.fromkeys(self.config["universe"].get("default_watchlist", [])))
        symbols = list(dict.fromkeys([*watchlist, "SPY"]))
        try:
            bars = self.vibe.fetch_lookback(symbols, cycle_now)
            liquidity = {symbol: self.vibe.average_daily_volume_usd(bars[symbol], cycle_now) for symbol in symbols}
            asset_classes = {symbol: "us_etf" if symbol in {"SPY", "QQQ", "XLK", "XLF"} else "us_equity" for symbol in symbols}
            quotes = self.quote_adapter.fetch_quotes(symbols, liquidity_usd=liquidity, asset_classes=asset_classes)
        except AdapterError as exc:
            event = {"event": "forward_cycle_failed_closed", "reason": str(exc), "stage": "market_data", "clock": clock.to_dict()}
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

        if hasattr(self.quote_adapter, "fetch_session_volumes") and clock.open_time:
            try:
                session_volumes = self.quote_adapter.fetch_session_volumes(symbols, clock.open_time, decision_now)
            except Exception as exc:
                session_volumes = {}
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
        exits = self._process_exits(quotes, clock)
        option_quotes, option_data_error = self._fetch_held_option_quotes()
        option_monitor_now = utc_now() if option_quotes else decision_now
        option_open_updates = [order.to_dict() for order in self.option_broker.process_open_orders(option_quotes, option_monitor_now)]
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
                "option_data_error": option_data_error,
            }
            event["portfolio"] = portfolio
            append_jsonl(self.root, "decisions.jsonl", event)
            write_heartbeat(self.root, "ok", event, now=decision_now)
            return event

        positions = self.broker.store.positions()
        snapshots: dict[str, dict[str, Any]] = {}
        baseline_candidates: list[dict[str, Any]] = []
        for symbol in watchlist:
            quote = quotes.get(symbol)
            if quote is None:
                continue
            snapshot = build_snapshot(symbol, quote, bars, clock, positions=positions, benchmark_quote=quotes.get("SPY"))
            snapshots[symbol] = snapshot
            baseline = decide_snapshot(snapshot, self.config)
            if baseline["action"] == "buy":
                baseline_candidates.append(baseline)
            else:
                append_jsonl(self.root, "decisions.jsonl", {"event": "baseline_decision", "decision": baseline, "snapshot": snapshot})

        max_candidates = int(self.integration_config.get("runtime", {}).get("max_candidates_per_cycle", 3))
        baseline_candidates.sort(key=lambda item: item["technical"]["relative_strength_20d"], reverse=True)
        selected = baseline_candidates[:max_candidates]
        shadow_results: list[dict[str, Any]] = []
        orders: list[dict[str, Any]] = []
        # Baseline execution is intentionally completed before network-bound
        # Exa and LLM shadow research. Shadow latency must not age the quote or
        # alter the active deterministic strategy's fill.
        for baseline in selected:
            symbol = baseline["ticker"]
            snapshot = snapshots[symbol]
            append_jsonl(self.root, "decisions.jsonl", {"event": "baseline_decision", "decision": baseline, "snapshot": snapshot})
            order = self._submit_baseline_entry(snapshot, quotes[symbol])
            if order is not None:
                orders.append(order)

        option_entries, option_decisions = self._process_option_entries(snapshots, quotes)

        for baseline in selected:
            symbol = baseline["ticker"]
            enriched = self._attach_news(snapshots[symbol])
            shadow = self.shadow_team.run(enriched).to_dict()
            append_jsonl(self.root, "shadow_decisions.jsonl", shadow)
            shadow_results.append(shadow)

        result = {
            "event": "forward_cycle_complete",
            "clock": clock.to_dict(),
            "quotes": len(quotes),
            "snapshots": len(snapshots),
            "baseline_candidates": len(baseline_candidates),
            "selected_candidates": [item["ticker"] for item in selected],
            "open_order_updates": open_updates,
            "exits": exits,
            "option_open_order_updates": option_open_updates,
            "option_exits": option_exits,
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

    def _submit_baseline_entry(self, snapshot: dict[str, Any], quote: Quote) -> dict[str, Any] | None:
        account = self.broker.store.account()
        positions = self.broker.store.positions()
        if snapshot["ticker"] in positions:
            return None
        buffer = float(self.integration_config.get("runtime", {}).get("order_notional_buffer_pct", 0.96))
        quantity = calculate_entry_quantity(account, positions, quote, self.config["risk"], notional_buffer_pct=buffer)
        if quantity <= 0:
            append_jsonl(self.root, "decisions.jsonl", {"event": "baseline_order_skipped", "ticker": snapshot["ticker"], "reason": "paper account has no capacity within position and shared caps"})
            return None
        execution_now = snapshot["decision_time"]
        order = self.broker.create_order(
            decision_id=snapshot["snapshot_id"],
            symbol=snapshot["ticker"],
            side="buy",
            order_type="limit",
            quantity=float(quantity),
            limit_price=quote.ask,
            quote_seen_at=quote.asof,
            thesis="relative_strength_v1 deterministic candidate",
            idempotency_key=f"relative_strength_v1:{snapshot['snapshot_id']}",
            now=execution_now,
        )
        submitted = self.broker.submit_order(order, quote, now=execution_now)
        journal = write_order_journal(self.root, submitted, note="forward baseline paper entry")
        return {"order": submitted.to_dict(), "journal_path": str(journal)}

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
        decisions = [decide_option_direction(snapshot, self.config) for snapshot in snapshots.values()]
        candidates = [item for item in decisions if item["action"] == "buy_to_open"]
        candidates.sort(key=lambda item: abs(float(snapshots[item["ticker"]]["technical_signals"]["relative_strength_20d"])), reverse=True)
        max_candidates = int(self.integration_config.get("runtime", {}).get("max_option_candidates_per_cycle", 1))
        candidates = candidates[:max_candidates]
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
            account = self.option_broker.store.base.account()
            equity_positions = self.option_broker.store.base.positions()
            option_positions = self.option_broker.store.positions()
            account_equity_at_cost = account.cash
            account_equity_at_cost += sum(position.average_price * position.quantity for position in equity_positions.values())
            account_equity_at_cost += sum(position.cost_basis() for position in option_positions.values())
            premium_budget = account_equity_at_cost * float(self.config["options_risk"].get("max_order_risk_pct_of_equity", 0.10)) * 0.95
            symbol = candidate["ticker"]
            try:
                selected = self.option_data.fetch_best_contract(
                    underlying=symbol,
                    underlying_price=quotes[symbol].last,
                    option_type=candidate["option_type"],
                    now=utc_now(),
                    max_premium_usd=premium_budget,
                )
            except Exception as exc:
                candidate["action"] = "no_trade"
                candidate["reasons"].append(f"option data failed closed: {type(exc).__name__}")
                append_jsonl(self.root, "audit.jsonl", {"event": "option_entry_failed_closed", "stage": "contract_selection", "ticker": symbol, "reason": f"{type(exc).__name__}: {exc}"})
                continue
            if selected is None:
                candidate["action"] = "no_trade"
                candidate["reasons"].append("no liquid contract met DTE/delta/spread filters")
                continue
            contract, option_quote = selected
            execution_now = utc_now()
            order = self.option_broker.create_order(
                decision_id=f"{candidate['snapshot_id']}:options",
                contract=contract,
                intent="buy_to_open",
                order_type="limit",
                quantity=1,
                limit_price=option_quote.ask,
                quote_seen_at=option_quote.updated_at,
                thesis="long_directional_options_v1 deterministic candidate",
                idempotency_key=f"long_directional_options_v1:{candidate['snapshot_id']}:{contract.option_id}",
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
        return entries, decisions

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
    service = ForwardPaperService(root)
    runtime = service.integration_config.get("runtime", {})
    scheduler = BlockingScheduler(timezone="America/New_York")

    def run_forward_cycle() -> None:
        _emit_runtime_event(_cycle_summary(service.run_once()))

    def run_research_cycle() -> None:
        result = service.run_hourly_research()
        _emit_runtime_event({"event": result.get("event"), "reason": result.get("reason")})

    def run_catalyst_cycle() -> None:
        result = service.run_catalyst_discovery()
        _emit_runtime_event(
            {
                "event": result.get("event"),
                "strategy": result.get("strategy", "exa_deepseek_catalyst_v1"),
                "candidate_count": result.get("candidate_count", 0),
                "ranked_candidates": len(result.get("ranked_candidates", [])),
                "decisions": len(result.get("decisions", [])),
                "paper_orders_created": result.get("paper_orders_created", 0),
                "reason": result.get("reason"),
            }
        )

    scheduler.add_job(run_forward_cycle, "interval", seconds=int(runtime.get("forward_cycle_seconds", 300)), id="forward-paper-cycle", max_instances=1, coalesce=True)
    if service.swarm.config.get("enabled", False):
        scheduler.add_job(run_research_cycle, "interval", seconds=int(runtime.get("research_cycle_seconds", 3600)), id="vibe-hourly-research", max_instances=1, coalesce=True)
    catalyst_profile = getattr(service, "config", {}).get("strategies", {}).get("exa_deepseek_catalyst_v1", {})
    catalyst_discovery = catalyst_profile.get("discovery", {})
    if catalyst_discovery.get("enabled", False):
        scheduler.add_job(
            run_catalyst_cycle,
            "interval",
            seconds=int(catalyst_discovery.get("cycle_seconds", 3600)),
            id="exa-deepseek-catalyst-discovery",
            max_instances=1,
            coalesce=True,
            next_run_time=datetime.now(timezone.utc),
        )
    lock = ProcessLock(Path(root) / "state" / "forward_service.lock")
    if not lock.acquire():
        raise RuntimeError("forward paper service is already running")

    def request_shutdown(signum: int, _frame: Any) -> None:
        _emit_runtime_event({"event": "forward_service_stop_requested", "signal": signum})
        if scheduler.running:
            scheduler.shutdown(wait=False)

    previous_handlers: dict[int, Any] = {}
    try:
        handled_signals = [signal.SIGINT, signal.SIGTERM]
        if hasattr(signal, "SIGBREAK"):
            handled_signals.append(signal.SIGBREAK)
        for signum in handled_signals:
            previous_handlers[signum] = signal.signal(signum, request_shutdown)
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
        _emit_runtime_event({"event": "forward_service_stop_requested", "signal": "KeyboardInterrupt"})
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)
        for signum, previous in previous_handlers.items():
            signal.signal(signum, previous)
        lock.release()
        _emit_runtime_event({"event": "forward_service_stopped"})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--readiness", action="store_true")
    parser.add_argument("--catalyst-once", action="store_true")
    parser.add_argument("--now")
    args = parser.parse_args()
    service = ForwardPaperService(args.root)
    if args.readiness:
        result = service.readiness()
    elif args.catalyst_once:
        result = service.run_catalyst_discovery(args.now)
    elif args.once:
        result = service.run_once(args.now)
    else:
        serve(args.root)
        return
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
