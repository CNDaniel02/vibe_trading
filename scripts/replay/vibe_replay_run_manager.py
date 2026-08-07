from __future__ import annotations

import argparse
import json
import shutil
import uuid
from collections import defaultdict
from datetime import timedelta
from pathlib import Path
from typing import Any

from scripts.adapters.errors import AdapterConfigurationError
from scripts.adapters.vibe_market_data_adapter import MarketBar, VibeMarketDataAdapter
from scripts.core.audit import append_jsonl
from scripts.core.config import assert_paper_mode, load_runtime_config
from scripts.core.models import Quote, parse_ts
from scripts.evaluation.calculate_metrics import calculate_metrics
from scripts.exit.evaluate_exit import evaluate_position_exit
from scripts.journal.write_trade_journal import write_order_journal
from scripts.research.snapshot_builder import build_snapshot
from scripts.risk.position_sizing import calculate_entry_quantity
from scripts.runtime.market_clock import UsEquityMarketClock
from scripts.simulation.paper_broker import PaperBroker
from scripts.strategies.relative_strength_v1 import decide_snapshot


class VibeReplayRunManager:
    """Point-in-time replay using the forward strategy/risk/fill/exit kernel."""

    def __init__(
        self,
        project_root: str | Path,
        start_date: str,
        end_date: str,
        symbols: list[str],
        *,
        adapter: VibeMarketDataAdapter | None = None,
        run_id: str | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.project_config = load_runtime_config(self.project_root)
        assert_paper_mode(self.project_config)
        self.replay_config = self.project_config["integrations"].get("historical_replay", {})
        self.start_date = start_date
        self.end_date = end_date
        self.symbols = list(dict.fromkeys([symbol.upper() for symbol in symbols] + ["SPY"]))
        self.adapter = adapter or VibeMarketDataAdapter(self.project_root, self.project_config["integrations"]["vibe"])
        self.clock = UsEquityMarketClock()
        self.run_root = self._create_run_root(run_id or f"replay_{uuid.uuid4().hex}")
        self.config = load_runtime_config(self.run_root)
        self.broker = PaperBroker(self.run_root, self.config)

    def run(self, max_events: int | None = None) -> dict[str, Any]:
        interval = str(self.replay_config.get("interval", "5m"))
        if self.replay_config.get("require_intraday_for_preclose_exit", True) and interval.upper() in {"1D", "1W", "1M"}:
            raise AdapterConfigurationError("daily bars cannot validate the pre-close exit rule without lookahead")
        intraday = self.adapter.fetch_bars(
            self.symbols,
            self.start_date,
            self.end_date,
            interval=interval,
            source=str(self.replay_config.get("source", "yahoo")),
        )
        history_start = (parse_ts(self.start_date) - timedelta(days=90)).date().isoformat()
        daily = self.adapter.fetch_bars(self.symbols, history_start, self.end_date, interval="1D", source=str(self.replay_config.get("source", "yahoo")))
        timeline: dict[str, dict[str, MarketBar]] = defaultdict(dict)
        for symbol, bars in intraday.items():
            for bar in bars:
                timeline[parse_ts(bar.timestamp).isoformat()][symbol] = bar
        timestamps = sorted(timeline, key=parse_ts)
        if max_events is not None:
            timestamps = timestamps[:max_events]

        decision_count = 0
        order_count = 0
        exit_count = 0
        last_quotes: dict[str, Quote] = {}
        session_volumes: dict[str, float] = defaultdict(float)
        for timestamp in timestamps:
            decision_time = (parse_ts(timestamp) + timedelta(seconds=int(self.replay_config.get("quote_delay_seconds", 1)))).isoformat()
            clock = self.clock.status(decision_time)
            if not clock.is_regular:
                continue
            for symbol, bar in timeline[timestamp].items():
                session_volumes[symbol] += max(0, bar.volume)
            quotes = self._quotes_for_timestamp(timeline[timestamp], daily, timestamp, session_volumes)
            last_quotes.update(quotes)
            self.broker.process_open_orders(last_quotes, decision_time)
            exit_count += self._process_exits(last_quotes, clock)
            if clock.minutes_to_close is not None and clock.minutes_to_close <= int(self.config["paper"].get("exit_before_close_minutes", 10)):
                self._record_portfolio(last_quotes, clock)
                continue
            positions = self.broker.store.positions()
            candidates: list[tuple[dict[str, Any], dict[str, Any], Quote]] = []
            for symbol in self.symbols:
                quote = quotes.get(symbol)
                if quote is None:
                    continue
                snapshot = build_snapshot(symbol, quote, daily, clock, positions=positions, benchmark_quote=quotes.get("SPY"))
                decision = decide_snapshot(snapshot, self.config)
                append_jsonl(self.run_root, "decisions.jsonl", {"event": "baseline_decision", "mode": "vibe_replay", "decision": decision, "snapshot": snapshot})
                decision_count += 1
                if decision["action"] == "buy" and symbol != "SPY":
                    candidates.append((decision, snapshot, quote))
            candidates.sort(key=lambda item: item[0]["technical"]["relative_strength_20d"], reverse=True)
            for _, snapshot, quote in candidates[: int(self.project_config["integrations"].get("runtime", {}).get("max_candidates_per_cycle", 3))]:
                if snapshot["ticker"] in self.broker.store.positions():
                    continue
                account = self.broker.store.account()
                positions = self.broker.store.positions()
                quantity = calculate_entry_quantity(
                    account,
                    positions,
                    quote,
                    self.config["risk"],
                    notional_buffer_pct=float(self.project_config["integrations"].get("runtime", {}).get("order_notional_buffer_pct", 0.96)),
                )
                if quantity < 1:
                    continue
                order = self.broker.create_order(
                    decision_id=snapshot["snapshot_id"],
                    symbol=snapshot["ticker"],
                    side="buy",
                    order_type="limit",
                    quantity=quantity,
                    limit_price=round(
                        quote.ask
                        + max(
                            quote.ask * float(self.config["costs"].get("slippage_bps", 0)) / 10000,
                            float(self.config["costs"].get("minimum_slippage_usd", 0)),
                        ),
                        4,
                    ),
                    quote_seen_at=quote.asof,
                    thesis="relative_strength_v1 replay candidate",
                    idempotency_key=f"relative_strength_v1:{snapshot['snapshot_id']}",
                )
                submitted = self.broker.submit_order(order, quote, now=decision_time)
                write_order_journal(self.run_root, submitted, note="Vibe point-in-time replay entry")
                order_count += 1
            self._record_portfolio(last_quotes, clock)

        result = {
            "event": "vibe_replay_complete",
            "run_root": str(self.run_root),
            "interval": interval,
            "synthetic_spread_bps": float(self.replay_config.get("synthetic_spread_bps", 10)),
            "events": len(timestamps),
            "decisions": decision_count,
            "entry_orders": order_count,
            "exit_orders": exit_count,
            "metrics": calculate_metrics(self.run_root),
            "limitations": ["Historical top-of-book is synthesized adversely around each observed bar open."],
        }
        append_jsonl(self.run_root, "audit.jsonl", result)
        return result

    def _quotes_for_timestamp(
        self,
        bars: dict[str, MarketBar],
        daily: dict[str, list[MarketBar]],
        timestamp: str,
        session_volumes: dict[str, float],
    ) -> dict[str, Quote]:
        spread_bps = float(self.replay_config.get("synthetic_spread_bps", 10))
        quotes: dict[str, Quote] = {}
        for symbol, bar in bars.items():
            half_spread = bar.open * spread_bps / 20_000
            previous = [item for item in daily.get(symbol, []) if parse_ts(item.timestamp).date() < parse_ts(timestamp).date()]
            adv = self.adapter.average_daily_volume_usd(daily.get(symbol, []), timestamp)
            quotes[symbol] = Quote(
                symbol,
                round(bar.open - half_spread, 4),
                round(bar.open + half_spread, 4),
                bar.open,
                parse_ts(timestamp).isoformat(),
                source=f"{bar.source}:synthetic_top_of_book",
                avg_daily_volume_usd=adv,
                asset_class=(
                    "us_etf"
                    if symbol
                    in {
                        str(value).upper()
                        for value in self.config["universe"].get(
                            "etf_symbols",
                            [],
                        )
                    }
                    else "us_equity"
                ),
                session_volume=session_volumes.get(symbol),
                previous_close=previous[-1].close if previous else None,
            )
        return quotes

    def _process_exits(self, quotes: dict[str, Quote], clock: Any) -> int:
        count = 0
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
            if not decision.should_exit or quote is None:
                continue
            order = self.broker.create_order(
                decision_id=f"replay-exit:{symbol}:{clock.session}:{decision.reason}",
                symbol=symbol,
                side="sell",
                order_type="market",
                quantity=position.quantity,
                limit_price=None,
                quote_seen_at=quote.asof,
                thesis=decision.reason,
                idempotency_key=f"replay-exit:{symbol}:{clock.session}:{decision.reason}",
            )
            submitted = self.broker.submit_order(order, quote, now=clock.asof)
            write_order_journal(self.run_root, submitted, note=f"Vibe replay exit: {decision.reason}")
            count += 1
        return count

    def _record_portfolio(self, quotes: dict[str, Quote], clock: Any) -> None:
        account = self.broker.store.account()
        positions = self.broker.store.positions()
        if any(symbol not in quotes for symbol in positions):
            equity = None
        else:
            equity = account.equity(positions, quotes)
        append_jsonl(
            self.run_root,
            "portfolio_snapshots.jsonl",
            {"event": "portfolio_snapshot", "session": clock.session, "asof": clock.asof, "cash": account.cash, "equity": equity, "positions": {key: value.to_dict() for key, value in positions.items()}},
        )

    def _create_run_root(self, run_id: str) -> Path:
        configured = Path(str(self.replay_config.get("run_root", "state/replays")))
        base = configured.resolve() if configured.is_absolute() else (self.project_root / configured).resolve()
        try:
            base.relative_to(self.project_root)
        except ValueError as exc:
            raise AdapterConfigurationError("replay root must stay inside project") from exc
        run_root = base / run_id
        if run_root.exists():
            raise AdapterConfigurationError(f"replay run already exists: {run_id}")
        shutil.copytree(self.project_root / "config", run_root / "config")
        (run_root / "state").mkdir()
        (run_root / "logs").mkdir()
        return run_root


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--symbols", default="AAPL,MSFT,NVDA,SPY")
    parser.add_argument("--max-events", type=int)
    args = parser.parse_args()
    manager = VibeReplayRunManager(args.root, args.start_date, args.end_date, args.symbols.split(","))
    print(json.dumps(manager.run(args.max_events), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
