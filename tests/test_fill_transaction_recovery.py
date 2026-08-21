from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts.core.config import load_runtime_config
from scripts.core.models import Quote
from scripts.options.models import OptionContract, OptionQuote
from scripts.options.paper_broker import OptionPaperBroker
from scripts.simulation.paper_broker import PaperBroker


NOW = "2026-07-06T15:00:00+00:00"
STATE_BOUNDARIES = (
    "paper_account.json",
    "positions",
    "orders",
    "daily_counters.json",
)


class SimulatedCrash(RuntimeError):
    pass


def _equity_quote(*, bid: float = 100.0, ask: float = 100.0) -> Quote:
    return Quote(
        symbol="AAPL",
        bid=bid,
        ask=ask,
        last=(bid + ask) / 2,
        asof=NOW,
        source="fixture",
        avg_daily_volume_usd=100_000_000,
        asset_class="us_equity",
    )


def _option_contract() -> OptionContract:
    return OptionContract(
        option_id="aapl-call-100",
        chain_id="aapl-chain",
        underlying="AAPL",
        option_type="call",
        strike_price=100,
        expiration_date="2026-08-07",
        multiplier=100,
        sellout_datetime="2026-08-07T19:30:00+00:00",
    )


def _option_quote(*, bid: float = 0.50, ask: float = 0.51) -> OptionQuote:
    return OptionQuote(
        option_id=_option_contract().option_id,
        bid=bid,
        ask=ask,
        mark=(bid + ask) / 2,
        updated_at=NOW,
        source="fixture",
        delta=0.45,
        gamma=0.04,
        theta=-0.03,
        vega=0.08,
        implied_volatility=0.25,
        volume=1000,
        open_interest=5000,
    )


def _inject_crash_after_state_write(monkeypatch, store, target: str, order_id: str) -> None:
    original = store.write_json
    crashed = False

    def write_json(name: str, data: Any) -> None:
        nonlocal crashed
        original(name, data)
        if crashed:
            return
        matches = name == target
        if target == "positions":
            matches = name in {"paper_positions.json", "paper_option_positions.json"}
        elif target == "orders":
            matches = name in {"paper_orders.json", "paper_option_orders.json"}
            matches = matches and data.get(order_id, {}).get("status") == "filled"
        elif target == "daily_counters.json":
            matches = matches and (
                int(data.get("trades", 0)) > 0
                or abs(float(data.get("daily_realized_pnl", 0))) > 0
            )
        if matches:
            crashed = True
            raise SimulatedCrash(f"crash after {name}")

    monkeypatch.setattr(store, "write_json", write_json)


def _pending_transaction(root: Path) -> tuple[str, dict[str, Any]]:
    ledger = json.loads(
        (root / "state" / "paper_fill_transactions.json").read_text(encoding="utf-8")
    )
    pending = [
        (transaction_id, record)
        for transaction_id, record in ledger["transactions"].items()
        if record["status"] == "prepared"
    ]
    assert len(pending) == 1
    return pending[0]


def _assert_transaction_committed_once(root: Path, transaction_id: str) -> None:
    ledger = json.loads(
        (root / "state" / "paper_fill_transactions.json").read_text(encoding="utf-8")
    )
    assert ledger["transactions"][transaction_id]["status"] == "committed"
    fill_logs = list((root / "logs").glob("paper*_fills.jsonl"))
    matches = 0
    for path in fill_logs:
        for line in path.read_text(encoding="utf-8").splitlines():
            if json.loads(line).get("fill_transaction_id") == transaction_id:
                matches += 1
    assert matches == 1


@pytest.mark.parametrize("boundary", STATE_BOUNDARIES)
def test_equity_entry_fill_recovers_exactly_once_after_each_boundary(
    paper_root: Path,
    monkeypatch,
    boundary: str,
) -> None:
    config = load_runtime_config(paper_root)
    broker = PaperBroker(paper_root, config)
    quote = _equity_quote()
    order = broker.create_order(
        decision_id="equity-entry",
        symbol="AAPL",
        side="buy",
        order_type="market",
        quantity=1,
        limit_price=None,
        quote_seen_at=NOW,
        now=NOW,
    )
    _inject_crash_after_state_write(monkeypatch, broker.store, boundary, order.order_id)

    with pytest.raises(SimulatedCrash):
        broker.submit_order(order, quote, NOW)

    transaction_id, transaction = _pending_transaction(paper_root)
    target_account = transaction["state_writes"][0]["data"]
    recovered = PaperBroker(paper_root, config)
    assert recovered.store.account().to_dict() == target_account
    assert recovered.store.positions()["AAPL"].quantity == 1
    assert recovered.store.orders()[order.order_id].status == "filled"
    assert recovered.store.daily_counters(NOW)["equity_trades"] == 1
    _assert_transaction_committed_once(paper_root, transaction_id)

    restarted = PaperBroker(paper_root, config)
    assert restarted.submit_order(order, quote, NOW).status == "filled"
    assert restarted.store.account().to_dict() == target_account
    assert restarted.store.positions()["AAPL"].quantity == 1
    assert restarted.store.daily_counters(NOW)["equity_trades"] == 1
    _assert_transaction_committed_once(paper_root, transaction_id)


@pytest.mark.parametrize("boundary", STATE_BOUNDARIES)
def test_equity_exit_fill_recovers_exactly_once_after_each_boundary(
    paper_root: Path,
    monkeypatch,
    boundary: str,
) -> None:
    config = load_runtime_config(paper_root)
    broker = PaperBroker(paper_root, config)
    entry_quote = _equity_quote()
    entry = broker.create_order(
        decision_id="equity-entry",
        symbol="AAPL",
        side="buy",
        order_type="market",
        quantity=1,
        limit_price=None,
        quote_seen_at=NOW,
        now=NOW,
    )
    assert broker.submit_order(entry, entry_quote, NOW).status == "filled"
    exit_quote = _equity_quote(bid=110.0, ask=110.1)
    exit_order = broker.create_order(
        decision_id="equity-exit",
        symbol="AAPL",
        side="sell",
        order_type="market",
        quantity=1,
        limit_price=None,
        quote_seen_at=NOW,
        now=NOW,
    )
    _inject_crash_after_state_write(monkeypatch, broker.store, boundary, exit_order.order_id)

    with pytest.raises(SimulatedCrash):
        broker.submit_order(exit_order, exit_quote, NOW)

    transaction_id, transaction = _pending_transaction(paper_root)
    target_account = transaction["state_writes"][0]["data"]
    recovered = PaperBroker(paper_root, config)
    assert recovered.store.account().to_dict() == target_account
    assert recovered.store.positions() == {}
    assert recovered.store.orders()[exit_order.order_id].status == "filled"
    counters = recovered.store.daily_counters(NOW)
    assert counters["equity_trades"] == 1
    assert counters["equity_realized_pnl"] == pytest.approx(target_account["realized_pnl"])
    _assert_transaction_committed_once(paper_root, transaction_id)

    restarted = PaperBroker(paper_root, config)
    assert restarted.submit_order(exit_order, exit_quote, NOW).status == "filled"
    assert restarted.store.account().to_dict() == target_account
    assert restarted.store.positions() == {}
    _assert_transaction_committed_once(paper_root, transaction_id)


@pytest.mark.parametrize("boundary", STATE_BOUNDARIES)
def test_option_entry_fill_recovers_exactly_once_after_each_boundary(
    paper_root: Path,
    monkeypatch,
    boundary: str,
) -> None:
    config = load_runtime_config(paper_root)
    broker = OptionPaperBroker(paper_root, config)
    quote = _option_quote()
    order = broker.create_order(
        decision_id="option-entry",
        contract=_option_contract(),
        intent="buy_to_open",
        order_type="market",
        quantity=1,
        limit_price=None,
        quote_seen_at=NOW,
        now=NOW,
    )
    _inject_crash_after_state_write(monkeypatch, broker.store.base, boundary, order.order_id)

    with pytest.raises(SimulatedCrash):
        broker.submit_order(order, quote, NOW)

    transaction_id, transaction = _pending_transaction(paper_root)
    target_account = transaction["state_writes"][0]["data"]
    recovered = OptionPaperBroker(paper_root, config)
    assert recovered.store.base.account().to_dict() == target_account
    assert recovered.store.positions()[order.contract.option_id].quantity == 1
    assert recovered.store.orders()[order.order_id].status == "filled"
    assert recovered.store.base.daily_counters(NOW)["option_trades"] == 1
    _assert_transaction_committed_once(paper_root, transaction_id)

    restarted = OptionPaperBroker(paper_root, config)
    assert restarted.submit_order(order, quote, NOW).status == "filled"
    assert restarted.store.base.account().to_dict() == target_account
    assert restarted.store.positions()[order.contract.option_id].quantity == 1
    assert restarted.store.base.daily_counters(NOW)["option_trades"] == 1
    _assert_transaction_committed_once(paper_root, transaction_id)


@pytest.mark.parametrize("boundary", STATE_BOUNDARIES)
def test_option_exit_fill_recovers_exactly_once_after_each_boundary(
    paper_root: Path,
    monkeypatch,
    boundary: str,
) -> None:
    config = load_runtime_config(paper_root)
    broker = OptionPaperBroker(paper_root, config)
    entry_quote = _option_quote()
    entry = broker.create_order(
        decision_id="option-entry",
        contract=_option_contract(),
        intent="buy_to_open",
        order_type="market",
        quantity=1,
        limit_price=None,
        quote_seen_at=NOW,
        now=NOW,
    )
    assert broker.submit_order(entry, entry_quote, NOW).status == "filled"
    exit_quote = _option_quote(bid=0.80, ask=0.81)
    exit_order = broker.create_order(
        decision_id="option-exit",
        contract=_option_contract(),
        intent="sell_to_close",
        order_type="market",
        quantity=1,
        limit_price=None,
        quote_seen_at=NOW,
        now=NOW,
    )
    _inject_crash_after_state_write(monkeypatch, broker.store.base, boundary, exit_order.order_id)

    with pytest.raises(SimulatedCrash):
        broker.submit_order(exit_order, exit_quote, NOW)

    transaction_id, transaction = _pending_transaction(paper_root)
    target_account = transaction["state_writes"][0]["data"]
    recovered = OptionPaperBroker(paper_root, config)
    assert recovered.store.base.account().to_dict() == target_account
    assert recovered.store.positions() == {}
    assert recovered.store.orders()[exit_order.order_id].status == "filled"
    counters = recovered.store.base.daily_counters(NOW)
    assert counters["option_trades"] == 1
    assert counters["option_realized_pnl"] == pytest.approx(target_account["realized_pnl"])
    _assert_transaction_committed_once(paper_root, transaction_id)

    restarted = OptionPaperBroker(paper_root, config)
    assert restarted.submit_order(exit_order, exit_quote, NOW).status == "filled"
    assert restarted.store.base.account().to_dict() == target_account
    assert restarted.store.positions() == {}
    _assert_transaction_committed_once(paper_root, transaction_id)
