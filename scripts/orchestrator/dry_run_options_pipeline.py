from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

from scripts.core.config import load_runtime_config
from scripts.evaluation.calculate_metrics import calculate_metrics
from scripts.options.models import OptionContract, OptionQuote
from scripts.options.paper_broker import OptionPaperBroker


DRY_RUN_TIME = "2026-07-06T15:00:00+00:00"


def run_options_dry_run(project_root: str | Path) -> dict[str, Any]:
    project_root = Path(project_root).resolve()
    with tempfile.TemporaryDirectory(prefix="options-paper-dry-run-") as temporary:
        root = Path(temporary)
        shutil.copytree(project_root / "config", root / "config")
        (root / "state").mkdir()
        (root / "logs").mkdir()
        config = load_runtime_config(root)
        broker = OptionPaperBroker(root, config)
        contract = OptionContract(
            option_id="fixture-aapl-put",
            chain_id="fixture-chain",
            underlying="AAPL",
            option_type="put",
            strike_price=100,
            expiration_date="2026-08-07",
            sellout_datetime="2026-08-07T19:30:00+00:00",
        )
        entry_quote = OptionQuote(
            option_id=contract.option_id,
            bid=0.95,
            ask=1.00,
            mark=0.975,
            updated_at=DRY_RUN_TIME,
            source="fixture",
            delta=-0.45,
            gamma=0.04,
            theta=-0.03,
            vega=0.08,
            implied_volatility=0.25,
            volume=1000,
            open_interest=5000,
        )
        entry = broker.create_order(
            decision_id="fixture-option-entry",
            contract=contract,
            intent="buy_to_open",
            order_type="limit",
            quantity=1,
            limit_price=1.01,
            quote_seen_at=entry_quote.updated_at,
            thesis="offline long-put fixture",
            now=DRY_RUN_TIME,
        )
        entry = broker.submit_order(entry, entry_quote, DRY_RUN_TIME)
        exit_time = "2026-07-06T16:00:00+00:00"
        exit_quote = OptionQuote(**{**entry_quote.to_dict(), "bid": 1.20, "ask": 1.25, "mark": 1.225, "updated_at": exit_time})
        closing = broker.create_order(
            decision_id="fixture-option-exit",
            contract=contract,
            intent="sell_to_close",
            order_type="market",
            quantity=1,
            limit_price=None,
            quote_seen_at=exit_quote.updated_at,
            thesis="offline close fixture",
            now=exit_time,
        )
        closing = broker.submit_order(closing, exit_quote, exit_time)
        report = {
            "dry_run": True,
            "used_network": False,
            "used_live_order_tools": False,
            "used_shared_virtual_cash": True,
            "entry": entry.to_dict(),
            "exit": closing.to_dict(),
            "paper_account": broker.store.base.account().to_dict(),
            "option_positions": {key: value.to_dict() for key, value in broker.store.positions().items()},
            "metrics": calculate_metrics(root),
        }
    output = project_root / "logs" / "options_pipeline_dry_run.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


if __name__ == "__main__":
    print(json.dumps(run_options_dry_run(Path(__file__).resolve().parents[2]), indent=2, sort_keys=True))
