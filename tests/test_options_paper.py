from __future__ import annotations

import json
from unittest.mock import patch

from scripts.adapters.robinhood_option_market_data_adapter import RobinhoodOptionMarketDataAdapter
from scripts.core.config import load_runtime_config
from scripts.core.models import Account, Position
from scripts.options.exit_policy import evaluate_option_exit
from scripts.options.fill_model import simulate_option_fill
from scripts.options.greeks import black_scholes_estimate
from scripts.options.models import OptionContract, OptionOrder, OptionPosition, OptionQuote
from scripts.options.paper_broker import OptionPaperBroker
from scripts.options.portfolio import aggregate_portfolio_greeks
from scripts.options.risk_gate import check_option_order, validate_option_quote
from scripts.options.strategy import decide_option_direction
from scripts.orchestrator.dry_run_options_pipeline import run_options_dry_run
from scripts.risk.shared_portfolio_risk import check_shared_entry, shared_entry_capacity


NOW = "2026-07-06T15:00:00+00:00"


def contract(option_type: str = "call") -> OptionContract:
    return OptionContract(
        option_id=f"aapl-{option_type}-100",
        chain_id="aapl-chain",
        underlying="AAPL",
        option_type=option_type,  # type: ignore[arg-type]
        strike_price=100,
        expiration_date="2026-08-07",
        multiplier=100,
        sellout_datetime="2026-08-07T19:30:00+00:00",
    )


def quote(option_type: str = "call", *, bid: float = 0.95, ask: float = 1.0, asof: str = NOW) -> OptionQuote:
    return OptionQuote(
        option_id=contract(option_type).option_id,
        bid=bid,
        ask=ask,
        mark=(bid + ask) / 2,
        updated_at=asof,
        source="fixture",
        delta=0.45 if option_type == "call" else -0.45,
        gamma=0.04,
        theta=-0.03,
        vega=0.08,
        implied_volatility=0.25,
        volume=1000,
        open_interest=5000,
    )


def order(option_type: str = "call", *, intent: str = "buy_to_open", limit_price: float | None = 1.01) -> OptionOrder:
    return OptionOrder(
        order_id="option-test",
        decision_id="decision-test",
        contract=contract(option_type),
        intent=intent,  # type: ignore[arg-type]
        quantity=1,
        order_type="limit" if limit_price is not None else "market",
        limit_price=limit_price,
        quote_seen_at=NOW,
        idempotency_key="option-test",
        created_at=NOW,
    )


def test_option_buy_fill_uses_ask_and_adverse_slippage(paper_root):
    config = load_runtime_config(paper_root)
    decision = simulate_option_fill(order(), quote(), config["options_costs"], NOW)
    assert decision.status == "filled"
    assert decision.fill is not None
    assert decision.fill.price == 1.01
    assert decision.fill.gross_amount == 101


def test_option_sell_fill_uses_bid_and_adverse_slippage(paper_root):
    config = load_runtime_config(paper_root)
    decision = simulate_option_fill(order(intent="sell_to_close", limit_price=None), quote(), config["options_costs"], NOW)
    assert decision.fill is not None
    assert decision.fill.price == 0.94


def test_option_limit_not_reached_remains_open(paper_root):
    config = load_runtime_config(paper_root)
    assert simulate_option_fill(order(limit_price=0.90), quote(), config["options_costs"], NOW).status == "open"
    assert simulate_option_fill(order(intent="sell_to_close", limit_price=1.05), quote(), config["options_costs"], NOW).status == "open"


def test_long_put_is_allowed_but_sell_to_open_is_rejected(paper_root):
    config = load_runtime_config(paper_root)
    put_order = order("put")
    approved = check_option_order(put_order, quote("put"), Account(2000, 2000), {}, {}, {}, {}, {"trades": 0}, config, NOW)
    assert approved.approved
    unsafe = order("put", intent="sell_to_open")
    rejected = check_option_order(unsafe, quote("put"), Account(2000, 2000), {}, {}, {}, {}, {"trades": 0}, config, NOW)
    assert not rejected.approved
    assert rejected.reason == "unsupported option intent"


def test_stale_or_missing_option_quote_fails_closed(paper_root):
    config = load_runtime_config(paper_root)
    missing = check_option_order(order(), None, Account(2000, 2000), {}, {}, {}, {}, {"trades": 0}, config, NOW)
    stale = check_option_order(
        order(), quote(asof="2026-07-06T14:58:00+00:00"), Account(2000, 2000), {}, {}, {}, {}, {"trades": 0}, config, NOW
    )
    assert not missing.approved and missing.reason == "missing option quote"
    assert not stale.approved and stale.reason == "stale option quote"


def test_true_future_option_quote_still_fails_closed(paper_root):
    config = load_runtime_config(paper_root)
    future = validate_option_quote(
        quote(asof="2026-07-06T15:00:05+00:00"),
        NOW,
        config,
    )

    assert not future.approved
    assert future.reason == "future option quote would create lookahead"


def test_option_entries_are_blocked_before_market_close(paper_root):
    config = load_runtime_config(paper_root)
    near_close = "2026-07-06T19:55:00+00:00"
    decision = check_option_order(
        order(),
        quote(asof=near_close),
        Account(2000, 2000),
        {},
        {},
        {},
        {},
        {"trades": 0},
        config,
        near_close,
    )
    assert not decision.approved
    assert decision.reason == "new entries blocked before market close"


def test_wide_option_spread_and_excess_premium_are_rejected(paper_root):
    config = load_runtime_config(paper_root)
    wide = check_option_order(order(), quote(bid=0.50, ask=1.0), Account(2000, 2000), {}, {}, {}, {}, {"trades": 0}, config, NOW)
    expensive_order = order(limit_price=3.0)
    expensive_quote = quote(bid=2.95, ask=3.0)
    expensive = check_option_order(expensive_order, expensive_quote, Account(2000, 2000), {}, {}, {}, {}, {"trades": 0}, config, NOW)
    assert not wide.approved and wide.reason == "option spread too wide"
    assert not expensive.approved and expensive.reason == "max option premium risk exceeded"


def test_duplicate_and_daily_option_limits_are_enforced(paper_root):
    config = load_runtime_config(paper_root)
    current = order()
    current.status = "open"
    duplicate = check_option_order(order(), quote(), Account(2000, 2000), {}, {}, {}, {"existing": current}, {"trades": 0}, config, NOW)
    daily = check_option_order(order(), quote(), Account(2000, 2000), {}, {}, {}, {}, {"trades": 2, "option_trades": 2}, config, NOW)
    assert not duplicate.approved and "duplicate" in duplicate.reason
    assert not daily.approved and daily.reason == "max daily option trades reached"


def test_expired_and_zero_dte_entries_are_rejected(paper_root):
    config = load_runtime_config(paper_root)
    expired_contract = OptionContract(
        option_id="expired",
        chain_id="chain",
        underlying="AAPL",
        option_type="put",
        strike_price=100,
        expiration_date="2026-07-06",
    )
    expired_order = order("put")
    expired_order.contract = expired_contract
    expired_quote = quote("put")
    expired_quote = OptionQuote(**{**expired_quote.to_dict(), "option_id": "expired"})
    decision = check_option_order(expired_order, expired_quote, Account(2000, 2000), {}, {}, {}, {}, {"trades": 0}, config, NOW)
    assert not decision.approved and "0DTE" in decision.reason


def test_option_broker_uses_shared_cash_and_independent_state(paper_root):
    config = load_runtime_config(paper_root)
    broker = OptionPaperBroker(paper_root, config)
    submitted = broker.submit_order(
        broker.create_order(
            decision_id="put-entry",
            contract=contract("put"),
            intent="buy_to_open",
            order_type="limit",
            quantity=1,
                limit_price=1.01,
            quote_seen_at=NOW,
            now=NOW,
        ),
        quote("put"),
        NOW,
    )
    assert submitted.status == "filled"
    assert broker.store.base.account().cash == 1899
    assert contract("put").option_id in broker.store.positions()
    assert json.loads((paper_root / "state" / "paper_positions.json").read_text(encoding="utf-8")) == {}
    recovered = OptionPaperBroker(paper_root, config)
    assert contract("put").option_id in recovered.store.positions()


def test_option_close_ignores_entry_liquidity_rules_and_does_not_consume_trade_limit(paper_root):
    config = load_runtime_config(paper_root)
    broker = OptionPaperBroker(paper_root, config)
    opened = broker.submit_order(
        broker.create_order(
            decision_id="option-entry",
            contract=contract(),
            intent="buy_to_open",
            order_type="limit",
            quantity=1,
            limit_price=1.01,
            quote_seen_at=NOW,
            now=NOW,
        ),
        quote(),
        NOW,
    )
    assert opened.status == "filled"

    illiquid_exit_quote = OptionQuote(
        option_id=contract().option_id,
        bid=0.80,
        ask=1.50,
        mark=1.15,
        updated_at=NOW,
        source="fixture",
        volume=0,
        open_interest=0,
    )
    closed = broker.submit_order(
        broker.create_order(
            decision_id="option-exit",
            contract=contract(),
            intent="sell_to_close",
            order_type="market",
            quantity=1,
            limit_price=None,
            quote_seen_at=NOW,
            now=NOW,
        ),
        illiquid_exit_quote,
        NOW,
    )
    assert closed.status == "filled"
    counters = broker.store.base.daily_counters(NOW)
    assert counters["trades"] == 1
    assert counters["option_trades"] == 1


def test_created_or_open_option_order_is_not_a_position(paper_root):
    config = load_runtime_config(paper_root)
    broker = OptionPaperBroker(paper_root, config)
    created = broker.create_order(
        decision_id="open-order",
        contract=contract(),
        intent="buy_to_open",
        order_type="limit",
        quantity=1,
        limit_price=0.90,
        quote_seen_at=NOW,
        now=NOW,
    )
    submitted = broker.submit_order(created, quote(), NOW)
    assert submitted.status == "open"
    assert broker.store.positions() == {}


def test_shared_total_risk_cap_blocks_two_lines_using_account(paper_root):
    config = load_runtime_config(paper_root)
    account = Account(cash=1000, initial_cash=2000)
    equities = {"MSFT": Position("MSFT", 10, 100, NOW, NOW)}
    decision = check_shared_entry(
        line="options",
        new_risk_usd=201,
        account=account,
        equity_positions=equities,
        option_positions={},
        equity_orders={},
        option_orders={},
        counters={"trades": 0},
        shared_config=config["shared_risk"],
    )
    assert not decision.approved
    assert decision.reason == "shared total deployed risk cap exceeded"


def test_shared_entry_capacity_reports_remaining_line_and_total_room(paper_root):
    config = load_runtime_config(paper_root)
    account = Account(cash=1000, initial_cash=2000)
    equities = {"MSFT": Position("MSFT", 10, 100, NOW, NOW)}
    capacity = shared_entry_capacity(
        line="options",
        account=account,
        equity_positions=equities,
        option_positions={},
        equity_orders={},
        option_orders={},
        shared_config=config["shared_risk"],
    )
    assert capacity == 200


def test_option_expiry_and_sellout_policy_forces_close():
    position = OptionPosition(contract(), 1, 1.0, NOW, NOW)
    near_sellout = evaluate_option_exit(position, quote(), "2026-08-07T19:05:00+00:00", {"exit_before_sellout_minutes": 30, "force_exit_dte": 2})
    assert near_sellout.should_exit
    assert "sellout" in near_sellout.reason


def test_black_scholes_reference_has_valid_call_and_put_greeks():
    call = black_scholes_estimate(option_type="call", spot=100, strike=100, years_to_expiry=30 / 365, volatility=0.25)
    put = black_scholes_estimate(option_type="put", spot=100, strike=100, years_to_expiry=30 / 365, volatility=0.25)
    assert 0 < call.delta < 1
    assert -1 < put.delta < 0
    assert call.gamma > 0 and call.theta_per_day < 0 and call.vega_per_vol_point > 0


def test_option_portfolio_greeks_use_contract_multiplier():
    position = OptionPosition(contract("put"), 1, 1.0, NOW, NOW)
    totals = aggregate_portfolio_greeks({position.contract.option_id: position}, {position.contract.option_id: quote("put")})
    assert totals["complete"] is True
    assert totals["delta"] == -45
    assert totals["theta_per_day"] == -3


def test_option_direction_thresholds_are_runtime_configurable(paper_root):
    config = load_runtime_config(paper_root)
    snapshot = {
        "snapshot_id": "option-threshold-call",
        "ticker": "AAPL",
        "market_session": "regular",
        "market_data": {"market_regime": "neutral", "binary_event_within_days": 30},
        "technical_signals": {
            "relative_strength_20d": 0.6,
            "price_change_1d_pct": 0.1,
            "price_change_5d_pct": 0.6,
            "volume_ratio": 0.5,
            "chase_score": 0.2,
        },
    }
    assert decide_option_direction(snapshot)["action"] == "no_trade"
    decision = decide_option_direction(snapshot, config)
    assert decision["action"] == "buy_to_open"
    assert decision["option_type"] == "call"


def test_lower_option_thresholds_preserve_risk_off_put_direction(paper_root):
    config = load_runtime_config(paper_root)
    snapshot = {
        "snapshot_id": "option-threshold-put",
        "ticker": "SPY",
        "market_session": "regular",
        "market_data": {"market_regime": "risk_off", "binary_event_within_days": 30},
        "technical_signals": {
            "relative_strength_20d": -0.3,
            "price_change_1d_pct": -0.2,
            "price_change_5d_pct": -1.1,
            "volume_ratio": 0.5,
            "chase_score": 0.2,
        },
    }
    decision = decide_option_direction(snapshot, config)
    assert decision["action"] == "buy_to_open"
    assert decision["option_type"] == "put"


def test_robinhood_option_selection_uses_post_fetch_observation_time(paper_root):
    config = load_runtime_config(paper_root)
    adapter = RobinhoodOptionMarketDataAdapter(
        {"enabled": True},
        config,
        paper_root,
    )
    chain = {
        "id": "chain",
        "symbol": "AAPL",
        "can_open_position": True,
        "trade_value_multiplier": "100",
        "underlying_instruments": ["equity-id"],
        "expiration_dates": ["2026-08-07"],
    }
    raw_contract = {
        "id": "option-id",
        "chain_id": "chain",
        "chain_symbol": "AAPL",
        "type": "call",
        "strike_price": "100",
        "expiration_date": "2026-08-07",
    }
    with (
        patch.object(adapter, "readiness", return_value={"ready": True}),
        patch.object(adapter.client, "get_option_chains", return_value={"data": {"chains": [chain]}}),
        patch.object(adapter.client, "get_option_instruments", return_value={"data": {"instruments": [raw_contract], "next": None}}),
        patch.object(
            adapter.client,
            "get_option_quotes",
            return_value={"data": {"results": [{"quote": {**quote().to_dict(), "instrument_id": "option-id", "bid_price": 0.95, "ask_price": 1.0, "mark_price": 0.975}}]}},
        ),
            patch(
                "scripts.adapters.robinhood_option_market_data_adapter.utc_now",
                return_value="2026-07-13T15:00:03+00:00",
            ) as observed_at,
            patch("scripts.adapters.robinhood_option_market_data_adapter.rank_contracts", return_value=[]) as rank,
        ):
        adapter.fetch_best_contract(
            underlying="AAPL",
            underlying_price=100,
            option_type="call",
            now=NOW,
        )
    observed_at.assert_called_once_with(timespec="microseconds")
    assert rank.call_args.args[2] == "2026-07-13T15:00:03+00:00"


def test_option_selection_diagnostics_explain_budget_shortfall(paper_root):
    config = load_runtime_config(paper_root)
    adapter = RobinhoodOptionMarketDataAdapter({"enabled": True}, config, paper_root)
    chain = {
        "id": "chain",
        "symbol": "AAPL",
        "can_open_position": True,
        "trade_value_multiplier": "100",
        "underlying_instruments": ["equity-id"],
        "expiration_dates": ["2026-08-07"],
    }
    raw_contract = {
        "id": "option-id",
        "chain_id": "chain",
        "chain_symbol": "AAPL",
        "type": "call",
        "strike_price": "100",
        "expiration_date": "2026-08-07",
    }
    with (
        patch.object(adapter, "readiness", return_value={"ready": True}),
        patch.object(adapter.client, "get_option_chains", return_value={"data": {"chains": [chain]}}),
        patch.object(adapter.client, "get_option_instruments", return_value={"data": {"instruments": [raw_contract], "next": None}}),
        patch.object(adapter.client, "get_option_quotes", return_value={"data": {"results": []}}),
        patch("scripts.adapters.robinhood_option_market_data_adapter.utc_now", return_value=NOW),
        patch(
            "scripts.adapters.robinhood_option_market_data_adapter.rank_contracts_with_diagnostics",
            return_value=([(contract(), quote())], {"rejections": {}, "accepted_before_premium_cap": 1}),
        ),
    ):
        selected, diagnostics = adapter.fetch_best_contract_with_diagnostics(
            underlying="AAPL",
            underlying_price=100,
            option_type="call",
            now=NOW,
            max_premium_usd=80,
        )
    assert selected is None
    assert diagnostics["minimum_eligible_premium_usd"] == 100
    assert diagnostics["minimum_budget_shortfall_usd"] == 20
    assert diagnostics["cheapest_eligible_contract"]["option_id"] == contract().option_id


def test_option_readiness_requires_all_used_readonly_tools(
    paper_root,
    monkeypatch,
):
    config = load_runtime_config(paper_root)
    adapter = RobinhoodOptionMarketDataAdapter(
        {"enabled": True},
        config,
        paper_root,
    )

    async def present():
        return object()

    async def incomplete_probe():
        return {
            "tool_names": sorted(
                RobinhoodOptionMarketDataAdapter.REQUIRED_TOOLS
                - {"get_option_quotes"}
            )
        }

    monkeypatch.setattr(adapter.client.store, "get_tokens", present)
    monkeypatch.setattr(adapter.client.store, "get_client_info", present)
    monkeypatch.setattr(adapter.client, "probe", incomplete_probe)
    status = adapter.readiness()
    assert status["ready"] is False
    assert status["missing_tools"] == ["get_option_quotes"]


def test_option_paper_broker_has_no_live_broker_methods(paper_root):
    broker = OptionPaperBroker(paper_root, load_runtime_config(paper_root))
    assert {"place_option_order", "review_option_order", "replace_option_order"}.isdisjoint(dir(broker))


def test_options_offline_dry_run_closes_loop_without_live_tools(paper_root):
    report = run_options_dry_run(paper_root)
    assert report["used_network"] is False
    assert report["used_live_order_tools"] is False
    assert report["entry"]["status"] == "filled"
    assert report["exit"]["status"] == "filled"
    assert report["option_positions"] == {}
    assert report["metrics"]["lines"]["options"]["closed_trade_count"] == 1
    assert report["metrics"]["lines"]["options"]["evidence_sufficient"] is False
    assert report["metrics"]["lines"]["options"]["promotion_eligible"] is False
    assert report["metrics"]["lines"]["equity"]["promotion_eligible"] is False
