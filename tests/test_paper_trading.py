from __future__ import annotations

import json

from scripts.broker.robinhood_readonly_adapter import LiveOrderToolBlocked, RobinhoodReadonlyAdapter
from scripts.core.config import load_runtime_config
from scripts.core.models import Order, Position, Quote
from scripts.core.state import JsonStateStore
from scripts.exit.evaluate_exit import should_exit_before_close
from scripts.orchestrator.run_paper_cycle import run_cycle
from scripts.risk.position_sizing import calculate_entry_quantity
from scripts.simulation.paper_broker import PaperBroker

NOW = "2026-07-04T14:00:30+00:00"


def quote(symbol: str = "SPY", bid: float = 100.0, ask: float = 100.1, asof: str = "2026-07-04T14:00:00+00:00") -> Quote:
    return Quote(
        symbol=symbol,
        bid=bid,
        ask=ask,
        last=(bid + ask) / 2,
        asof=asof,
        source="test",
        avg_daily_volume_usd=100_000_000,
        asset_class="us_etf",
    )


def broker(root) -> PaperBroker:
    return PaperBroker(root, load_runtime_config(root))


def make_order(pb: PaperBroker, q: Quote, **overrides) -> Order:
    data = {
        "decision_id": overrides.pop("decision_id", "d1"),
        "symbol": q.symbol,
        "side": overrides.pop("side", "buy"),
        "order_type": overrides.pop("order_type", "limit"),
        "quantity": overrides.pop("quantity", 1),
        "limit_price": overrides.pop("limit_price", q.ask),
        "quote_seen_at": q.asof,
        "thesis": "test",
        "idempotency_key": overrides.pop("idempotency_key", "d1"),
    }
    data.update(overrides)
    return pb.create_order(**data)


def test_live_order_tools_never_called_in_paper_mode(paper_root):
    adapter = RobinhoodReadonlyAdapter()
    try:
        adapter.place_equity_order(symbol="SPY")
        raised = False
    except LiveOrderToolBlocked:
        raised = True
    assert raised

    result = run_cycle(paper_root, {"SPY": quote()}, mode="forward")
    assert result["event"] == "paper_cycle_complete"


def test_paper_cash_is_separate_from_robinhood_cash(paper_root):
    fake_robinhood_cash = 999_999
    pb = broker(paper_root)
    assert pb.store.account().cash == 2000
    assert fake_robinhood_cash != pb.store.account().cash


def test_fractional_position_size_rounds_down_to_configured_increment(paper_root):
    config = load_runtime_config(paper_root)
    pb = broker(paper_root)
    q = quote(bid=327.7, ask=327.8)
    quantity = calculate_entry_quantity(pb.store.account(), {}, q, config["risk"], notional_buffer_pct=0.96)
    assert quantity == 1.464
    assert quantity * q.ask <= 2000 * 0.25 * 0.96


def test_order_does_not_fill_when_limit_not_reached(paper_root):
    pb = broker(paper_root)
    q = quote(bid=100.8, ask=101.0)
    order = make_order(pb, q, limit_price=100.9)
    result = pb.submit_order(order, q, now=NOW)
    assert result.status == "open"
    assert pb.store.positions() == {}


def test_buy_fill_uses_ask_not_midpoint(paper_root):
    pb = broker(paper_root)
    q = quote(bid=100.9, ask=101.0)
    order = make_order(pb, q, order_type="market", limit_price=None)
    result = pb.submit_order(order, q, now=NOW)
    assert result.status == "filled"
    assert result.average_fill_price > q.ask
    assert result.average_fill_price > q.mid


def test_sell_fill_uses_bid_not_midpoint(paper_root):
    store = JsonStateStore(paper_root)
    store.save_positions({"SPY": Position("SPY", 1, 90, NOW, NOW)})
    pb = broker(paper_root)
    q = quote(bid=99.0, ask=99.1)
    order = make_order(pb, q, side="sell", order_type="market", limit_price=None)
    result = pb.submit_order(order, q, now=NOW)
    assert result.status == "filled"
    assert result.average_fill_price < q.bid
    assert result.average_fill_price < q.mid


def test_slippage_applied_against_agent(paper_root):
    pb = broker(paper_root)
    q = quote(bid=100, ask=100)
    buy = make_order(pb, q, order_type="market", limit_price=None)
    buy_result = pb.submit_order(buy, q, now=NOW)
    assert buy_result.average_fill_price > 100


def test_no_lookahead_data(paper_root):
    pb = broker(paper_root)
    q = quote(asof="2026-07-04T14:01:00+00:00")
    order = make_order(pb, q)
    result = pb.submit_order(order, q, now=NOW)
    assert result.status == "rejected"
    assert "lookahead" in result.reject_reason


def test_no_all_in(paper_root):
    pb = broker(paper_root)
    q = quote(bid=100, ask=100)
    order = make_order(pb, q, quantity=20)
    result = pb.submit_order(order, q, now=NOW)
    assert result.status == "rejected"
    assert result.reject_reason == "all-in order blocked"


def test_max_position_size(paper_root):
    pb = broker(paper_root)
    q = quote(bid=100, ask=100)
    order = make_order(pb, q, quantity=6)
    result = pb.submit_order(order, q, now=NOW)
    assert result.status == "rejected"
    assert result.reject_reason == "max order size exceeded"


def test_max_daily_trades(paper_root):
    (paper_root / "state" / "daily_counters.json").write_text(json.dumps({"date": "2026-07-04", "trades": 4}), encoding="utf-8")
    pb = broker(paper_root)
    q = quote()
    order = make_order(pb, q)
    result = pb.submit_order(order, q, now=NOW)
    assert result.status == "rejected"
    assert result.reject_reason == "max daily trades reached"


def test_no_average_down(paper_root):
    store = JsonStateStore(paper_root)
    store.save_positions({"SPY": Position("SPY", 1, 110, NOW, NOW)})
    pb = broker(paper_root)
    q = quote(bid=99.95, ask=100.0)
    order = make_order(pb, q)
    result = pb.submit_order(order, q, now=NOW)
    assert result.status == "rejected"
    assert result.reject_reason == "average down blocked"


def test_duplicate_order_blocked(paper_root):
    pb = broker(paper_root)
    q = quote()
    existing = make_order(pb, q, decision_id="same", idempotency_key="same")
    existing.status = "open"
    orders = pb.store.orders()
    orders[existing.order_id] = existing
    pb.store.save_orders(orders)
    second = make_order(pb, q, decision_id="second", idempotency_key="same")
    result = pb.submit_order(second, q, now=NOW)
    assert result.status == "rejected"
    assert result.reject_reason == "duplicate idempotency key"


def test_stale_quote_rejected(paper_root):
    pb = broker(paper_root)
    q = quote(asof="2026-07-04T13:00:00+00:00")
    order = make_order(pb, q)
    result = pb.submit_order(order, q, now=NOW)
    assert result.status == "rejected"
    assert result.reject_reason == "stale quote"


def test_missing_quote_fail_closed(paper_root):
    pb = broker(paper_root)
    q = quote()
    order = make_order(pb, q)
    result = pb.submit_order(order, None, now=NOW)
    assert result.status == "rejected"
    assert result.reject_reason == "missing quote"


def test_state_recovery_after_restart(paper_root):
    pb = broker(paper_root)
    q = quote()
    order = make_order(pb, q)
    first = pb.submit_order(order, q, now=NOW)
    assert first.status == "filled"
    recovered = PaperBroker(paper_root, load_runtime_config(paper_root))
    assert "SPY" in recovered.store.positions()
    assert first.order_id in recovered.store.orders()


def test_exit_before_market_close():
    assert should_exit_before_close("2026-07-04T19:55:00+00:00")
    assert not should_exit_before_close("2026-07-04T15:00:00+00:00")


def test_audit_log_is_append_only(paper_root):
    pb = broker(paper_root)
    q = quote()
    order = make_order(pb, q, decision_id="a1")
    pb.submit_order(order, q, now=NOW)
    path = paper_root / "logs" / "audit.jsonl"
    before = path.read_text(encoding="utf-8")
    second = make_order(pb, q, decision_id="a2", idempotency_key="a2")
    pb.submit_order(second, q, now=NOW)
    after = path.read_text(encoding="utf-8")
    assert after.startswith(before)
    assert len(after) > len(before)
