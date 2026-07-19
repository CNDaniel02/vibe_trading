from __future__ import annotations

import argparse
import json
import time
import os
from pathlib import Path
from typing import Any

from apscheduler.schedulers.blocking import BlockingScheduler

from scripts.adapters.alpaca_market_data_adapter import AlpacaMarketDataAdapter
from scripts.adapters.errors import AdapterError
from scripts.adapters.exa_news_adapter import ExaNewsAdapter
from scripts.adapters.vibe_market_data_adapter import VibeMarketDataAdapter
from scripts.adapters.vibe_research_swarm_adapter import VibeResearchSwarmAdapter
from scripts.agents.api_investment_team import ApiInvestmentTeam
from scripts.core.audit import append_jsonl
from scripts.core.config import assert_paper_mode, load_runtime_config
from scripts.core.models import Quote, utc_now
from scripts.exit.evaluate_exit import evaluate_position_exit
from scripts.journal.write_trade_journal import write_order_journal
from scripts.llm import build_provider
from scripts.research.snapshot_builder import build_snapshot
from scripts.risk.position_sizing import calculate_entry_quantity
from scripts.runtime.heartbeat import write_heartbeat
from scripts.runtime.market_clock import UsEquityMarketClock
from scripts.runtime.process_lock import ProcessLock
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
        self.quote_adapter = AlpacaMarketDataAdapter(forward.get("alpaca", {}))
        self.news_adapter = ExaNewsAdapter(forward.get("exa", {}))
        self.swarm = VibeResearchSwarmAdapter(self.root, integrations.get("vibe", {}))
        self.clock = UsEquityMarketClock()
        self.broker = PaperBroker(self.root, self.config)
        provider, tracker = build_provider(self.config["llm"], self.root)
        self.shadow_team = ApiInvestmentTeam(self.root, self.config, provider, tracker)
        self.tracker = tracker

    def readiness(self) -> dict[str, Any]:
        vibe_status = self.vibe.runtime.status().to_dict()
        llm_provider = str(self.config["llm"].get("provider", "mock"))
        llm_key_env = str(self.config["llm"].get("api", {}).get("api_key_env", "OPENAI_API_KEY"))
        llm_ready = llm_provider == "mock" or bool(os.getenv(llm_key_env))
        alpaca = self.quote_adapter.readiness()
        exa = self.news_adapter.readiness()
        return {
            "paper_mode": True,
            "live_trading": False,
            "vibe": vibe_status,
            "alpaca": alpaca,
            "exa": exa,
            "llm_provider": llm_provider,
            "llm_api_key_env": llm_key_env if llm_provider == "api" else None,
            "llm_ready": llm_ready,
            "ready_for_forward_quotes": vibe_status["ready"] and alpaca["ready"],
            "ready_for_news_shadow": exa["ready"] and llm_ready,
            "ready_for_full_forward_evaluation": vibe_status["ready"] and alpaca["ready"] and exa["ready"] and llm_ready,
        }

    def run_once(self, now: str | None = None) -> dict[str, Any]:
        now = now or utc_now()
        clock = self.clock.status(now)
        if not clock.is_regular:
            event = {"event": "forward_cycle_skipped", "reason": f"market session is {clock.market_session}", "clock": clock.to_dict()}
            append_jsonl(self.root, "audit.jsonl", event)
            write_heartbeat(self.root, "idle", event, now=now)
            return event

        watchlist = list(dict.fromkeys(self.config["universe"].get("default_watchlist", [])))
        symbols = list(dict.fromkeys([*watchlist, "SPY"]))
        try:
            bars = self.vibe.fetch_lookback(symbols, now)
            liquidity = {symbol: self.vibe.average_daily_volume_usd(bars[symbol], now) for symbol in symbols}
            asset_classes = {symbol: "us_etf" if symbol in {"SPY", "QQQ", "XLK", "XLF"} else "us_equity" for symbol in symbols}
            quotes = self.quote_adapter.fetch_quotes(symbols, liquidity_usd=liquidity, asset_classes=asset_classes)
        except AdapterError as exc:
            event = {"event": "forward_cycle_failed_closed", "reason": str(exc), "stage": "market_data", "clock": clock.to_dict()}
            append_jsonl(self.root, "audit.jsonl", event)
            write_heartbeat(self.root, "failed", event, now=now)
            return event

        open_updates = [order.to_dict() for order in self.broker.process_open_orders(quotes, now)]
        exits = self._process_exits(quotes, clock)
        if clock.minutes_to_close is not None and clock.minutes_to_close <= int(self.config["paper"].get("exit_before_close_minutes", 10)):
            portfolio = self._record_portfolio_snapshot(quotes, clock)
            event = {"event": "forward_cycle_exit_only", "clock": clock.to_dict(), "open_order_updates": open_updates, "exits": exits}
            event["portfolio"] = portfolio
            append_jsonl(self.root, "decisions.jsonl", event)
            write_heartbeat(self.root, "ok", event, now=now)
            return event

        positions = self.broker.store.positions()
        snapshots: dict[str, dict[str, Any]] = {}
        baseline_candidates: list[dict[str, Any]] = []
        for symbol in watchlist:
            quote = quotes.get(symbol)
            if quote is None:
                continue
            snapshot = build_snapshot(symbol, quote, bars, clock, positions=positions)
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
        for baseline in selected:
            symbol = baseline["ticker"]
            snapshot = snapshots[symbol]
            enriched = self._attach_news(snapshot)
            baseline = decide_snapshot(enriched, self.config)
            append_jsonl(self.root, "decisions.jsonl", {"event": "baseline_decision", "decision": baseline, "snapshot": enriched})
            shadow = self.shadow_team.run(enriched).to_dict()
            append_jsonl(self.root, "shadow_decisions.jsonl", shadow)
            shadow_results.append(shadow)
            order = self._submit_baseline_entry(enriched, quotes[symbol])
            if order is not None:
                orders.append(order)

        result = {
            "event": "forward_cycle_complete",
            "clock": clock.to_dict(),
            "quotes": len(quotes),
            "snapshots": len(snapshots),
            "baseline_candidates": len(baseline_candidates),
            "selected_candidates": [item["ticker"] for item in selected],
            "open_order_updates": open_updates,
            "exits": exits,
            "orders": orders,
            "shadow_decisions": shadow_results,
            "usage": self.tracker.summary(),
        }
        result["portfolio"] = self._record_portfolio_snapshot(quotes, clock)
        append_jsonl(self.root, "audit.jsonl", result)
        write_heartbeat(self.root, "ok", {"event": result["event"], "orders": len(orders)}, now=now)
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

    def _attach_news(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        enriched = dict(snapshot)
        try:
            news, metadata = self.news_adapter.search(snapshot["ticker"], snapshot["data_cutoff_time"])
        except AdapterError as exc:
            news = []
            metadata = [{"source": "exa", "source_tier": 4, "error": str(exc)}]
        enriched["available_news"] = news
        enriched["source_metadata"] = [*snapshot["source_metadata"], *metadata]
        return enriched

    def _submit_baseline_entry(self, snapshot: dict[str, Any], quote: Quote) -> dict[str, Any] | None:
        account = self.broker.store.account()
        positions = self.broker.store.positions()
        if snapshot["ticker"] in positions:
            return None
        buffer = float(self.integration_config.get("runtime", {}).get("order_notional_buffer_pct", 0.96))
        quantity = calculate_entry_quantity(account, positions, quote, self.config["risk"], notional_buffer_pct=buffer)
        if quantity < 1:
            append_jsonl(self.root, "decisions.jsonl", {"event": "baseline_order_skipped", "ticker": snapshot["ticker"], "reason": "paper account cannot buy one share within position cap"})
            return None
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
        )
        submitted = self.broker.submit_order(order, quote, now=snapshot["decision_time"])
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
            )
            submitted = self.broker.submit_order(order, quote, now=clock.asof)
            journal = write_order_journal(self.root, submitted, note=f"forward paper exit: {decision.reason}")
            exits.append({"decision": decision.to_dict(), "order": submitted.to_dict(), "journal_path": str(journal)})
        return exits

    def _record_portfolio_snapshot(self, quotes: dict[str, Quote], clock: Any) -> dict[str, Any]:
        account = self.broker.store.account()
        positions = self.broker.store.positions()
        missing_quotes = sorted(symbol for symbol in positions if symbol not in quotes)
        equity = None if missing_quotes else account.equity(positions, quotes)
        unrealized = None
        if equity is not None:
            unrealized = equity - account.cash - sum(position.average_price * position.quantity for position in positions.values())
        snapshot = {
            "event": "portfolio_snapshot",
            "session": clock.session,
            "asof": clock.asof,
            "cash": round(account.cash, 4),
            "equity": round(equity, 4) if equity is not None else None,
            "realized_pnl": round(account.realized_pnl, 4),
            "unrealized_pnl": round(unrealized, 4) if unrealized is not None else None,
            "positions": {symbol: position.to_dict() for symbol, position in positions.items()},
            "missing_position_quotes": missing_quotes,
        }
        append_jsonl(self.root, "portfolio_snapshots.jsonl", snapshot)
        return snapshot


def serve(root: str | Path) -> None:
    service = ForwardPaperService(root)
    runtime = service.integration_config.get("runtime", {})
    scheduler = BlockingScheduler(timezone="America/New_York")
    scheduler.add_job(service.run_once, "interval", seconds=int(runtime.get("forward_cycle_seconds", 300)), id="forward-paper-cycle", max_instances=1, coalesce=True)
    if service.swarm.config.get("enabled", False):
        scheduler.add_job(service.run_hourly_research, "interval", seconds=int(runtime.get("research_cycle_seconds", 3600)), id="vibe-hourly-research", max_instances=1, coalesce=True)
    lock = ProcessLock(Path(root) / "state" / "forward_service.lock")
    if not lock.acquire():
        raise RuntimeError("forward paper service is already running")
    try:
        write_heartbeat(root, "idle", {"event": "forward_service_started"})
        scheduler.start()
    finally:
        lock.release()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--readiness", action="store_true")
    parser.add_argument("--now")
    args = parser.parse_args()
    service = ForwardPaperService(args.root)
    if args.readiness:
        result = service.readiness()
    elif args.once:
        result = service.run_once(args.now)
    else:
        serve(args.root)
        return
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
