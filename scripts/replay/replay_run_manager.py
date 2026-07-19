from __future__ import annotations

import argparse
import json
from pathlib import Path

from scripts.agents.investment_team import run_investment_team
from scripts.core.audit import append_jsonl
from scripts.core.config import assert_paper_mode, load_runtime_config
from scripts.replay.historical_data_adapter import CsvHistoricalMarketDataAdapter
from scripts.replay.virtual_clock import VirtualClock
from scripts.research.simple_research import pick_first_valid_candidate
from scripts.simulation.paper_broker import PaperBroker


class ReplayRunManager:
    def __init__(self, root: str | Path, quotes_csv: str | Path) -> None:
        self.root = Path(root)
        self.config = load_runtime_config(self.root)
        assert_paper_mode(self.config)
        self.adapter = CsvHistoricalMarketDataAdapter(quotes_csv)
        self.clock = VirtualClock()
        self.broker = PaperBroker(self.root, self.config)

    def run(self, max_events: int | None = None) -> dict:
        events = self.adapter.events()
        if max_events is not None:
            events = events[:max_events]

        decisions = 0
        orders = 0
        latest_quotes = {}
        for event in events:
            now = self.clock.advance_to(event.timestamp)
            latest_quotes[event.symbol] = event.quote
            decision = run_investment_team(
                event.symbol,
                event.quote,
                {"now": now, "max_spread_bps": self.config["universe"].get("max_spread_bps", 25)},
            )
            append_jsonl(self.root, "decisions.jsonl", {"event": "agent_team_decision", "mode": "historical", "decision": decision.to_dict()})
            decisions += 1
            if decision.recommendation != "candidate":
                continue
            candidate = pick_first_valid_candidate(latest_quotes, [event.symbol])
            if candidate is None:
                continue
            candidate["decision_id"] = decision.decision_id
            candidate["decision_time"] = now
            candidate["quote_seen_at"] = event.quote.asof
            candidate["thesis"] = decision.thesis
            order = self.broker.create_order(
                decision_id=candidate["decision_id"],
                symbol=candidate["symbol"],
                side=candidate["side"],
                order_type=candidate["order_type"],
                quantity=candidate["quantity"],
                limit_price=candidate["limit_price"],
                quote_seen_at=candidate["quote_seen_at"],
                thesis=candidate["thesis"],
                idempotency_key=candidate["decision_id"],
            )
            self.broker.submit_order(order, event.quote, now=now)
            orders += 1

        result = {
            "event": "historical_replay_complete",
            "events": len(events),
            "decisions": decisions,
            "orders_created": orders,
            "clock": self.clock.current_time,
        }
        append_jsonl(self.root, "audit.jsonl", result)
        return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--quotes-csv", required=True)
    parser.add_argument("--max-events", type=int)
    args = parser.parse_args()
    result = ReplayRunManager(args.root, args.quotes_csv).run(args.max_events)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
