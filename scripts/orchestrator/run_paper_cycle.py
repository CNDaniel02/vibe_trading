from __future__ import annotations

import argparse
import json
from pathlib import Path

from scripts.core.audit import append_jsonl
from scripts.core.config import assert_paper_mode, load_runtime_config
from scripts.core.models import Quote, utc_now
from scripts.journal.write_trade_journal import write_order_journal
from scripts.simulation.paper_broker import PaperBroker
from scripts.strategies.relative_strength_v1 import STRATEGY_NAME, select_paper_candidate


def load_quotes(path: str | Path) -> dict[str, Quote]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return {symbol: Quote(**payload) for symbol, payload in raw.items()}


def run_cycle(root: str | Path, quotes: dict[str, Quote], mode: str = "forward") -> dict:
    root = Path(root)
    config = load_runtime_config(root)
    assert_paper_mode(config)
    broker = PaperBroker(root, config)
    candidate = select_paper_candidate(quotes, config["universe"].get("default_watchlist", []))
    if candidate is None:
        event = {"event": "no_candidate", "mode": mode, "ts": utc_now()}
        append_jsonl(root, "decisions.jsonl", event)
        return event

    append_jsonl(root, "decisions.jsonl", {"event": "candidate", "mode": mode, "strategy": STRATEGY_NAME, "candidate": candidate})
    order = broker.create_order(
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
    filled_or_open = broker.submit_order(order, quotes.get(order.symbol), now=candidate["decision_time"])
    journal_path = write_order_journal(root, filled_or_open, note=f"{mode} paper cycle")
    return {
        "event": "paper_cycle_complete",
        "mode": mode,
        "order": filled_or_open.to_dict(),
        "journal_path": str(journal_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--quotes-json", required=True)
    parser.add_argument("--mode", choices=["historical", "forward"], default="forward")
    args = parser.parse_args()
    result = run_cycle(args.root, load_quotes(args.quotes_json), args.mode)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
