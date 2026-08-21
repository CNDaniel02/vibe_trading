from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from scripts.core.config import load_runtime_config
from scripts.discovery.ai_gated_pipeline import AiGatedPaperPipeline
from scripts.llm.mock_provider import MockProvider
from scripts.llm.usage_tracker import UsageTracker
from scripts.options.paper_broker import OptionPaperBroker
from scripts.orchestrator import forward_paper_service as forward_service_module
from scripts.simulation.paper_broker import PaperBroker


REGULAR_NOW = "2026-07-13T15:00:00+00:00"
OPEN_EXECUTION_NOW = "2026-07-13T13:32:00+00:00"
NEXT_CLOSE_EXIT = "2026-07-14T19:50:00+00:00"

SIGNED_BUCKETS = {
    "return_lt_minus_5_pct": 0.02,
    "return_minus_5_to_minus_2_pct": 0.04,
    "return_minus_2_to_minus_0_5_pct": 0.09,
    "return_minus_0_5_to_plus_0_5_pct": 0.15,
    "return_plus_0_5_to_plus_2_pct": 0.30,
    "return_plus_2_to_plus_5_pct": 0.25,
    "return_gt_plus_5_pct": 0.15,
}


class _MustNotDiscover:
    def collect_seed_candidates(self, *_args, **_kwargs):
        raise AssertionError("entry-frozen strategy must not start discovery")


class _NoNews:
    pass


class _NoOptions:
    @staticmethod
    def fetch_quotes(_option_ids):
        return {}


def test_legacy_executors_are_entry_frozen_by_default(paper_root: Path) -> None:
    config = load_runtime_config(paper_root)

    assert (
        config["strategies"]["long_directional_options_v2_weighted"][
            "new_entries_enabled"
        ]
        is False
    )
    assert (
        config["strategies"]["ai_gated_technical_v1"]["new_entries_enabled"]
        is False
    )


def test_forward_service_preserves_live_allocator_monitor_clock(
    paper_root: Path,
) -> None:
    observed: list[str | None] = []
    service = object.__new__(forward_service_module.ForwardPaperService)
    service.root = paper_root
    service.ai_instrument_allocator_pipeline = SimpleNamespace(
        monitor_only=lambda now: observed.append(now)
        or {"event": "allocator-monitor-test"}
    )

    service.run_ai_instrument_allocator_monitor()

    assert observed == [None]


def test_readiness_cli_never_constructs_stateful_service(
    paper_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = {
        "paper_mode": True,
        "live_trading": False,
        "ready_for_ai_instrument_allocator_paper": True,
    }
    monkeypatch.setattr(
        sys,
        "argv",
        ["forward_paper_service", "--root", str(paper_root), "--readiness"],
    )
    monkeypatch.setattr(
        forward_service_module,
        "run_healthcheck",
        lambda _root: report,
    )
    monkeypatch.setattr(
        forward_service_module,
        "ForwardPaperService",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("readiness must not construct the stateful service")
        ),
    )

    forward_service_module.main()

    assert json.loads(capsys.readouterr().out) == report


def test_entry_frozen_ai_pipeline_keeps_shadow_research_and_monitor_result(
    paper_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class NoExecutionQuoteDiscovery(_AllocatorResearchDiscovery):
        def fetch_current_quote(self, *_args, **_kwargs):
            raise AssertionError("frozen shadow decisions must not refresh execution quotes")

    config = load_runtime_config(paper_root)
    tracker = UsageTracker()
    pipeline = AiGatedPaperPipeline(
        paper_root,
        config,
        MockProvider(tracker),
        tracker,
        discovery_adapter=NoExecutionQuoteDiscovery(REGULAR_NOW),
        news_adapter=_AllocatorResearchNews(),
        option_data=_NoOptions(),
    )
    monkeypatch.setattr(
        pipeline,
        "_publish_signal",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("frozen shadow decisions must not publish executable signals")
        ),
    )

    result = pipeline.run(REGULAR_NOW)

    assert result["event"] == "ai_gated_cycle_complete"
    assert result["paper_orders_created"] == 0
    assert result["model_calls"] == 4
    assert result["new_entries_enabled"] is False
    assert result["decisions"][0]["execution"]["status"] == "shadow_only"
    assert pipeline.broker.store.orders() == {}
    assert pipeline.option_broker.store.orders() == {}
    assert result["monitor"]["event"] == "ai_gated_monitor_complete"
    assert result["live_order_tools_called"] is False


def test_explicit_sleeve_cash_isolated_and_never_resets_existing_state(
    paper_root: Path,
) -> None:
    config = load_runtime_config(paper_root)
    namespace = "ai_instrument_allocator_v1"

    equity = PaperBroker(
        paper_root,
        config,
        namespace=namespace,
        initial_cash=10_000,
    )
    options = OptionPaperBroker(
        paper_root,
        config,
        namespace=namespace,
        initial_cash=10_000,
    )

    assert equity.store.account().to_dict() == options.store.base.account().to_dict()
    assert equity.store.account().initial_cash == 10_000
    assert PaperBroker(paper_root, config).store.account().initial_cash == 2_000

    account = equity.store.account()
    account.cash = 9_876.54
    equity.store.save_account(account, REGULAR_NOW)

    restarted = PaperBroker(
        paper_root,
        config,
        namespace=namespace,
        initial_cash=99_999,
    )
    assert restarted.store.account().cash == 9_876.54
    assert restarted.store.account().initial_cash == 10_000


def test_entry_frozen_weighted_options_never_start_market_data_or_order_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = forward_service_module.ForwardPaperService.__new__(
        forward_service_module.ForwardPaperService
    )
    service.root = tmp_path
    service.config = {
        "paper": {"strategy_lines": {"options": True}},
        "strategies": {
            "long_directional_options_v2_weighted": {
                "new_entries_enabled": False
            }
        },
    }
    service.integration_config = {"runtime": {"max_option_candidates_per_cycle": 3}}
    service.option_broker = SimpleNamespace(
        store=SimpleNamespace(
            positions=lambda: (_ for _ in ()).throw(
                AssertionError("entry-frozen strategy must not inspect entry capacity")
            ),
            orders=lambda: {},
        )
    )
    service.option_data = SimpleNamespace(
        upcoming_earnings=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("entry-frozen strategy must not call option data")
        )
    )
    weighted = {
        "action": "buy_to_open",
        "ticker": "TLT",
        "snapshot_id": "frozen-options",
        "score": 0.9,
        "option_type": "put",
        "reasons": [],
    }
    baseline = {
        "action": "no_trade",
        "ticker": "TLT",
        "snapshot_id": "frozen-options",
    }
    monkeypatch.setattr(
        forward_service_module,
        "decide_weighted_option_direction",
        lambda *_args: dict(weighted),
    )
    monkeypatch.setattr(
        forward_service_module,
        "decide_option_direction",
        lambda *_args: dict(baseline),
    )

    entries, decisions = service._process_option_entries({"TLT": {}}, {}, REGULAR_NOW)

    assert entries == []
    assert decisions[0]["action"] == "buy_to_open"
    assert decisions[0]["execution_status"] == "entry_frozen"


def _signed_signal(**overrides):
    signal = {
        "action": "propose_trade",
        "ticker": "AAPL",
        "horizon": "next_close",
        "signed_return_probability_buckets": dict(SIGNED_BUCKETS),
        "probability_status": "uncalibrated",
        "thesis": "Grounded fixture thesis.",
        "supporting_evidence": ["Company guidance increased."],
        "source_urls": ["https://company.example/guidance"],
        "contrary_evidence": [],
        "data_gaps": [],
        "entry_condition": "Fresh quote confirms the setup.",
        "entry_now": True,
        "invalidation_condition": "Guidance is withdrawn.",
        "thesis_valid_until": "2026-07-15T20:00:00+00:00",
        "max_holding_trading_days": 1,
        "no_trade_reason": None,
    }
    signal.update(overrides)
    return signal


def _anchored_signal(**overrides):
    return {
        **_signed_signal(),
        "forecast_reference_price": 100.0,
        "forecast_reference_time": REGULAR_NOW,
        **overrides,
    }


def test_signed_return_signal_requires_complete_sum_to_one_uncalibrated_buckets() -> None:
    from scripts.decision.signed_return_signal import validate_signed_return_signal

    validate_signed_return_signal(_signed_signal())

    missing = _signed_signal()
    del missing["signed_return_probability_buckets"]["return_gt_plus_5_pct"]
    with pytest.raises(ValueError, match="exactly the configured signed buckets"):
        validate_signed_return_signal(missing)

    wrong_sum = _signed_signal()
    wrong_sum["signed_return_probability_buckets"]["return_gt_plus_5_pct"] = 0.25
    with pytest.raises(ValueError, match="sum to 1"):
        validate_signed_return_signal(wrong_sum)

    calibrated = _signed_signal(probability_status="calibrated")
    with pytest.raises(ValueError, match="uncalibrated"):
        validate_signed_return_signal(calibrated)


def test_python_derives_direction_and_conservative_magnitude_from_signed_buckets() -> None:
    from scripts.decision.signed_return_signal import derive_signal_summary

    summary = derive_signal_summary(_signed_signal())

    assert summary["bullish_probability"] == pytest.approx(0.70)
    assert summary["bearish_probability"] == pytest.approx(0.15)
    assert summary["neutral_probability"] == pytest.approx(0.15)
    assert summary["direction"] == "bullish"
    assert summary["dominant_signed_bucket"] == "return_plus_0_5_to_plus_2_pct"
    assert summary["conservative_move_pct"] == pytest.approx(0.7142857143)
    assert summary["conservative_move_method"] == "directional_lower_tail_mean"
    assert summary["conservative_tail_fraction"] == 0.5
    assert [item["bucket"] for item in summary["conservative_scenarios"]] == [
        "return_plus_0_5_to_plus_2_pct",
        "return_plus_2_to_plus_5_pct",
    ]
    assert summary["probability_status"] == "uncalibrated"


def test_derived_magnitude_uses_directional_lower_tail_scenarios() -> None:
    from scripts.decision.signed_return_signal import derive_signal_summary

    buckets = {
        "return_lt_minus_5_pct": 0.02,
        "return_minus_5_to_minus_2_pct": 0.03,
        "return_minus_2_to_minus_0_5_pct": 0.05,
        "return_minus_0_5_to_plus_0_5_pct": 0.30,
        "return_plus_0_5_to_plus_2_pct": 0.20,
        "return_plus_2_to_plus_5_pct": 0.20,
        "return_gt_plus_5_pct": 0.20,
    }

    summary = derive_signal_summary(
        _signed_signal(signed_return_probability_buckets=buckets)
    )

    assert summary["direction"] == "bullish"
    assert summary["dominant_signed_bucket"] == "return_plus_0_5_to_plus_2_pct"
    assert summary["conservative_move_pct"] == pytest.approx(1.0)


def test_allocator_signal_schema_has_no_model_selected_instrument() -> None:
    from scripts.llm.schemas import AI_ALLOCATOR_SIGNAL_OUTPUT_SCHEMA, validate_schema

    validate_schema(_signed_signal(), AI_ALLOCATOR_SIGNAL_OUTPUT_SCHEMA)
    assert "instrument" not in AI_ALLOCATOR_SIGNAL_OUTPUT_SCHEMA["properties"]
    assert "direction_probability" not in AI_ALLOCATOR_SIGNAL_OUTPUT_SCHEMA["properties"]
    assert "magnitude_distribution" not in AI_ALLOCATOR_SIGNAL_OUTPUT_SCHEMA["properties"]


def test_allocator_thinking_is_only_enabled_for_overnight_challenge_and_decision(
    paper_root: Path,
) -> None:
    config = load_runtime_config(paper_root)["llm"]["api"]
    thinking = config["thinking"]["agents"]

    assert thinking["ai_allocator_challenge_agent"] == {"type": "enabled"}
    assert thinking["ai_allocator_decision_manager"] == {"type": "enabled"}
    assert thinking["ai_allocator_fast_challenge_agent"] == {"type": "disabled"}
    assert thinking["ai_allocator_fast_decision_manager"] == {"type": "disabled"}


def _allocator_snapshot(*, with_event: bool = True) -> dict:
    event = {
        "ticker": "AAPL",
        "headline": "Apple raises forward guidance",
        "published_at": "2026-07-13T14:30:00+00:00",
        "event_at": "2026-07-13T14:25:00+00:00",
        "first_seen_at": "2026-07-13T14:31:00+00:00",
        "retrieved_at": REGULAR_NOW,
        "source": "company.example",
        "source_tier": 1,
        "ticker_relevance": 1.0,
        "direction": "positive",
        "novelty": 0.9,
        "already_priced_in": False,
        "confidence": 0.9,
        "url": "https://company.example/guidance",
        "highlights": ["Guidance increased."],
    }
    return {
        "snapshot_id": "allocator-AAPL",
        "decision_time": REGULAR_NOW,
        "data_cutoff_time": REGULAR_NOW,
        "ticker": "AAPL",
        "market_session": "regular",
        "market_data": {
            "quote": {
                "symbol": "AAPL",
                "bid": 100.0,
                "ask": 100.05,
                "last": 100.02,
                "asof": REGULAR_NOW,
            },
            "market_regime": "neutral",
        },
        "technical_signals": {
            "relative_strength_20d": 3.0,
            "price_change_1d_pct": 1.0,
            "price_change_5d_pct": 4.0,
            "volume_ratio": 1.4,
            "chase_score": 0.2,
        },
        "available_news": [event] if with_event else [],
        "source_metadata": [
            {
                "source": "company.example",
                "source_tier": 1,
                "retrieved_at": REGULAR_NOW,
            }
        ],
    }


def test_allocator_team_uses_stage_specific_agents_and_never_selects_instrument(
    paper_root: Path,
) -> None:
    from scripts.agents.ai_instrument_allocator_team import AiInstrumentAllocatorTeam

    config = load_runtime_config(paper_root)
    tracker = UsageTracker()
    team = AiInstrumentAllocatorTeam(config, MockProvider(tracker), tracker)
    ranking = team.rank(
        snapshot_id="allocator-cycle",
        decision_time=REGULAR_NOW,
        candidates=[
            {
                "ticker": "AAPL",
                "eligible": True,
                "pre_score": 0.8,
                "market_context": {"technical_signals": {"price_change_1d_pct": 1.0}},
                "events": [],
            }
        ],
    )
    analysis = team.analyze(_allocator_snapshot(), ranking["ranked_candidates"][0], stage="overnight")

    assert "instrument" not in ranking["ranked_candidates"][0]
    assert "instrument" not in analysis["signal"]
    assert analysis["signal"]["action"] == "propose_trade"
    assert analysis["signal"]["forecast_reference_price"] == 100.02
    assert analysis["signal"]["forecast_reference_time"] == REGULAR_NOW
    assert analysis["fail_closed"] is False
    assert [record.agent_name for record in tracker.records] == [
        "ai_allocator_ranker",
        "ai_allocator_news_agent",
        "ai_allocator_challenge_agent",
        "ai_allocator_decision_manager",
    ]


def test_allocator_team_enforces_challenge_veto_as_no_trade(paper_root: Path) -> None:
    from scripts.agents.ai_instrument_allocator_team import AiInstrumentAllocatorTeam

    config = load_runtime_config(paper_root)
    tracker = UsageTracker()
    team = AiInstrumentAllocatorTeam(config, MockProvider(tracker), tracker)
    analysis = team.analyze(
        _allocator_snapshot(with_event=False),
        {"ticker": "AAPL", "score": 0.8, "rationale": "fixture", "risk_flags": []},
        stage="fast",
    )

    assert analysis["challenge"]["veto_recommended"] is True
    assert analysis["signal"]["action"] == "no_trade"
    assert analysis["signal"]["entry_now"] is False
    assert [record.agent_name for record in tracker.records] == [
        "ai_allocator_fast_news_agent",
        "ai_allocator_fast_challenge_agent",
        "ai_allocator_fast_decision_manager",
    ]


def test_signed_return_signal_rejects_non_finite_probability() -> None:
    from scripts.decision.signed_return_signal import validate_signed_return_signal

    signal = _signed_signal()
    signal["signed_return_probability_buckets"][
        "return_plus_0_5_to_plus_2_pct"
    ] = float("nan")

    with pytest.raises(ValueError, match="finite"):
        validate_signed_return_signal(signal)


@pytest.mark.parametrize(
    "invalid_case",
    ["null_valid_until", "missing_invalidation", "expired", "horizon_mismatch"],
)
def test_allocator_team_fails_closed_for_invalid_actionable_mandate(
    paper_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_case: str,
) -> None:
    from scripts.agents.ai_instrument_allocator_team import AiInstrumentAllocatorTeam

    config = load_runtime_config(paper_root)
    tracker = UsageTracker()
    team = AiInstrumentAllocatorTeam(config, MockProvider(tracker), tracker)
    original_call = team._call

    def invalidating_call(agent_name, payload, schema):
        value = original_call(agent_name, payload, schema)
        if agent_name.endswith("decision_manager"):
            value = dict(value)
            if invalid_case == "null_valid_until":
                value["thesis_valid_until"] = None
            elif invalid_case == "missing_invalidation":
                value.pop("invalidation_condition", None)
            elif invalid_case == "expired":
                value["thesis_valid_until"] = "2026-07-13T14:59:59+00:00"
            else:
                value["horizon"] = "next_close"
                value["max_holding_trading_days"] = 2
        return value

    monkeypatch.setattr(team, "_call", invalidating_call)
    analysis = team.analyze(
        _allocator_snapshot(),
        {"ticker": "AAPL", "score": 0.8, "rationale": "fixture", "risk_flags": []},
        stage="fast",
    )

    assert analysis["fail_closed"] is True
    assert analysis["signal"]["action"] == "no_trade"
    assert analysis["signal"]["entry_now"] is False
    assert "actionable signal" in analysis["signal"]["no_trade_reason"]


def _option_contract(option_id: str, expiration: str, option_type: str = "call"):
    from scripts.options.models import OptionContract

    return OptionContract(
        option_id=option_id,
        chain_id="chain-AAPL",
        underlying="AAPL",
        option_type=option_type,
        strike_price=100.0,
        expiration_date=expiration,
    )


def _option_quote(option_id: str, *, bid: float = 1.0, ask: float = 1.01):
    from scripts.options.models import OptionQuote

    return OptionQuote(
        option_id=option_id,
        bid=bid,
        ask=ask,
        mark=(bid + ask) / 2,
        updated_at=REGULAR_NOW,
        source="fixture",
        delta=0.50,
        gamma=0.03,
        theta=-0.04,
        vega=0.12,
        implied_volatility=0.30,
        volume=5_000,
        open_interest=10_000,
    )


def test_option_hard_spread_is_two_percent_and_preferred_is_one_point_five(
    paper_root: Path,
) -> None:
    from scripts.options.risk_gate import validate_option_quote

    config = load_runtime_config(paper_root)
    assert config["options_universe"]["preferred_max_spread_pct"] == 0.015
    assert config["options_universe"]["max_spread_pct"] == 0.02

    too_wide = _option_quote("wide", bid=0.99, ask=1.02)
    assert validate_option_quote(too_wide, REGULAR_NOW, config).reason == "option spread too wide"


def test_robinhood_adapter_returns_bounded_candidates_across_expirations(
    paper_root: Path,
) -> None:
    from scripts.adapters.robinhood_option_market_data_adapter import (
        RobinhoodOptionMarketDataAdapter,
    )

    config = load_runtime_config(paper_root)
    adapter = RobinhoodOptionMarketDataAdapter({"enabled": True}, config, paper_root)
    expirations = ["2026-08-07", "2026-08-14", "2026-08-21"]
    chain = {
        "id": "chain-AAPL",
        "symbol": "AAPL",
        "can_open_position": True,
        "trade_value_multiplier": "100",
        "underlying_instruments": ["equity-AAPL"],
        "expiration_dates": expirations,
    }

    async def instruments(*, expiration_date, **_kwargs):
        index = expirations.index(expiration_date)
        return {
            "data": {
                "instruments": [
                    {
                        "id": f"call-{index}",
                        "chain_id": "chain-AAPL",
                        "chain_symbol": "AAPL",
                        "type": "call",
                        "strike_price": "100",
                        "expiration_date": expiration_date,
                    }
                ],
                "next": None,
            }
        }

    async def quotes(option_ids):
        return {
            "data": {
                "results": [
                    {
                        "quote": {
                            **_option_quote(option_id).to_dict(),
                            "instrument_id": option_id,
                            "bid_price": 1.0,
                            "ask_price": 1.01,
                            "mark_price": 1.005,
                        }
                    }
                    for option_id in option_ids
                ]
            }
        }

    with (
        patch.object(adapter, "readiness", return_value={"ready": True}),
        patch.object(
            adapter.client,
            "get_option_chains",
            return_value={"data": {"chains": [chain]}},
        ),
        patch.object(adapter.client, "get_option_instruments", side_effect=instruments),
        patch.object(adapter.client, "get_option_quotes", side_effect=quotes),
        patch(
            "scripts.adapters.robinhood_option_market_data_adapter.utc_now",
            return_value=REGULAR_NOW,
        ),
    ):
        candidates, diagnostics = adapter.fetch_contract_candidates(
            underlying="AAPL",
            underlying_price=100.0,
            option_type="call",
            now=REGULAR_NOW,
            min_dte=21,
            target_dte=30,
            max_dte=45,
            max_premium_usd=200,
        )

    assert len(candidates) == 3
    assert diagnostics["expirations_considered"] == expirations
    assert diagnostics["accepted_after_premium_cap"] == 3
    assert all(item[0].expiration_date in expirations for item in candidates)


def test_option_scenario_repricing_uses_spot_time_iv_and_reports_vega() -> None:
    from scripts.options.scenario_pricing import reprice_option_scenarios

    contract = _option_contract("call-reprice", "2026-08-21")
    quote = _option_quote("call-reprice", bid=4.95, ask=5.05)
    costs = {
        "slippage_bps": 20,
        "minimum_slippage_usd_per_contract": 0.01,
        "commission_per_contract_usd": 0,
        "price_tick_usd": 0.01,
    }
    up = reprice_option_scenarios(
        contract,
        quote,
        spot=100,
        now=REGULAR_NOW,
        elapsed_calendar_days=1,
        move_pct=3.0,
        iv_shifts=[-0.05, 0.0, 0.05],
        costs=costs,
    )
    flat_later = reprice_option_scenarios(
        contract,
        quote,
        spot=100,
        now=REGULAR_NOW,
        elapsed_calendar_days=5,
        move_pct=0.0,
        iv_shifts=[0.0],
        costs=costs,
    )

    assert up["method"] == "midpoint_anchored_black_scholes_repricing"
    assert up["greeks"]["vega"] == 0.12
    by_iv = {item["iv_shift"]: item for item in up["scenarios"]}
    assert by_iv[0.05]["repriced_mid"] > by_iv[-0.05]["repriced_mid"]
    assert up["conservative_exit_bid"] > flat_later["conservative_exit_bid"]
    assert up["probability_ev_available"] is False
    assert up["probability_ev_usd"] is None


def test_option_scenario_and_fill_use_the_same_tick_side_of_cutoff() -> None:
    from scripts.options.fill_model import simulate_option_fill
    from scripts.options.models import OptionOrder
    from scripts.options.scenario_pricing import reprice_option_scenarios

    contract = _option_contract("call-tick-cutoff", "2026-08-21")
    quote = _option_quote("call-tick-cutoff", bid=3.00, ask=3.01)
    costs = {
        "slippage_bps": 0,
        "minimum_slippage_usd_per_contract": 0.01,
        "commission_per_contract_usd": 0,
        "price_tick_usd": 0.01,
    }
    repricing = reprice_option_scenarios(
        contract,
        quote,
        spot=100,
        now=REGULAR_NOW,
        elapsed_calendar_days=1,
        move_pct=1.0,
        iv_shifts=[0.0],
        costs=costs,
    )
    buy = simulate_option_fill(
        OptionOrder(
            order_id="buy-tick-cutoff",
            decision_id="decision-tick-cutoff",
            contract=contract,
            intent="buy_to_open",
            quantity=1,
            order_type="market",
            limit_price=None,
        ),
        quote,
        costs,
        REGULAR_NOW,
    )

    assert buy.status == "filled"
    assert repricing["entry_executable_ask"] == buy.fill.price == 3.05

    scenario = repricing["scenarios"][0]
    projected_bid = scenario["repriced_mid"] - (quote.ask - quote.bid) / 2
    exit_quote = replace(
        quote,
        bid=projected_bid,
        ask=projected_bid + 0.01,
        mark=projected_bid + 0.005,
    )
    sell = simulate_option_fill(
        OptionOrder(
            order_id="sell-tick-cutoff",
            decision_id="decision-tick-cutoff",
            contract=contract,
            intent="sell_to_close",
            quantity=1,
            order_type="market",
            limit_price=None,
        ),
        exit_quote,
        costs,
        REGULAR_NOW,
    )

    assert sell.status == "filled"
    assert scenario["executable_exit_bid"] == sell.fill.price


def _bearish_signal() -> dict:
    buckets = {
        "return_lt_minus_5_pct": 0.15,
        "return_minus_5_to_minus_2_pct": 0.35,
        "return_minus_2_to_minus_0_5_pct": 0.25,
        "return_minus_0_5_to_plus_0_5_pct": 0.10,
        "return_plus_0_5_to_plus_2_pct": 0.08,
        "return_plus_2_to_plus_5_pct": 0.05,
        "return_gt_plus_5_pct": 0.02,
    }
    return _signed_signal(
        signed_return_probability_buckets=buckets,
        thesis="Grounded negative catalyst.",
    )


def _neutral_signal() -> dict:
    buckets = {
        "return_lt_minus_5_pct": 0.02,
        "return_minus_5_to_minus_2_pct": 0.05,
        "return_minus_2_to_minus_0_5_pct": 0.08,
        "return_minus_0_5_to_plus_0_5_pct": 0.70,
        "return_plus_0_5_to_plus_2_pct": 0.08,
        "return_plus_2_to_plus_5_pct": 0.05,
        "return_gt_plus_5_pct": 0.02,
    }
    return _signed_signal(signed_return_probability_buckets=buckets)


def _underlying_quote():
    from scripts.core.models import Quote

    return Quote(
        "AAPL",
        bid=100.0,
        ask=100.05,
        last=100.02,
        asof=REGULAR_NOW,
        source="fixture",
        avg_daily_volume_usd=500_000_000,
    )


def _account_state(nav: float = 10_000) -> dict:
    return {
        "nav_usd": nav,
        "cash_usd": nav,
        "equity_deployed_usd": 0.0,
        "options_deployed_usd": 0.0,
        "open_position_count": 0,
    }


def test_allocator_selects_equity_for_bullish_signal_when_no_option_clears_hurdle(
    paper_root: Path,
) -> None:
    from scripts.decision.instrument_allocator import allocate_instrument

    config = load_runtime_config(paper_root)
    allocation = allocate_instrument(
        _anchored_signal(),
        _underlying_quote(),
        [],
        _account_state(),
        config,
        REGULAR_NOW,
        planned_exit_at=NEXT_CLOSE_EXIT,
    )

    assert allocation["status"] == "selected"
    assert allocation["selected_instrument"]["instrument_type"] == "equity"
    assert allocation["selected_instrument"]["ticker"] == "AAPL"
    assert allocation["probability_ev_available"] is False
    assert allocation["probability_ev_usd"] is None
    assert allocation["raw_probability_used_for_ev"] is False


def test_allocator_compares_equity_and_option_on_frozen_deterministic_risk_score(
    paper_root: Path,
) -> None:
    from scripts.decision.instrument_allocator import allocate_instrument

    buckets = {
        "return_lt_minus_5_pct": 0.02,
        "return_minus_5_to_minus_2_pct": 0.01,
        "return_minus_2_to_minus_0_5_pct": 0.02,
        "return_minus_0_5_to_plus_0_5_pct": 0.05,
        "return_plus_0_5_to_plus_2_pct": 0.0,
        "return_plus_2_to_plus_5_pct": 0.50,
        "return_gt_plus_5_pct": 0.40,
    }
    allocation = allocate_instrument(
        _anchored_signal(signed_return_probability_buckets=buckets),
        _underlying_quote(),
        [
            (
                _option_contract("aapl-call-risk-score", "2026-08-21"),
                _option_quote("aapl-call-risk-score", bid=2.78, ask=2.82),
            )
        ],
        _account_state(),
        load_runtime_config(paper_root),
        REGULAR_NOW,
        planned_exit_at=NEXT_CLOSE_EXIT,
    )

    by_type = {item["instrument_type"]: item for item in allocation["considered"]}
    assert by_type["call"]["conservative_net_return_pct"] > by_type["equity"][
        "conservative_net_return_pct"
    ]
    assert by_type["equity"]["selection_score"] > by_type["call"]["selection_score"]
    assert allocation["selected_instrument"]["instrument_type"] == "equity"
    assert allocation["selection_policy_version"] == "deterministic_risk_adjusted_v1"
    for candidate in by_type.values():
        assert candidate["scenario_pnl_usd"] > 0
        assert candidate["scenario_return_on_account_nav"] > 0
        assert candidate["deterministic_risk_usd"] > 0
        assert candidate["deterministic_risk_pct_of_nav"] > 0
        assert candidate["selection_score"] == pytest.approx(
            candidate["scenario_return_on_account_nav"]
            / candidate["deterministic_risk_pct_of_nav"]
        )
        assert candidate["selection_score_method"] == (
            "conservative_scenario_pnl_over_deterministic_risk_v1"
        )


@pytest.mark.parametrize(
    "invalid_case",
    ["null_valid_until", "missing_invalidation", "expired", "horizon_mismatch"],
)
def test_allocator_returns_structured_no_trade_for_invalid_actionable_mandate(
    paper_root: Path,
    invalid_case: str,
) -> None:
    from scripts.decision.instrument_allocator import allocate_instrument

    signal = _anchored_signal()
    if invalid_case == "null_valid_until":
        signal["thesis_valid_until"] = None
    elif invalid_case == "missing_invalidation":
        signal.pop("invalidation_condition")
    elif invalid_case == "expired":
        signal["thesis_valid_until"] = "2026-07-13T14:59:59+00:00"
    else:
        signal["max_holding_trading_days"] = 2

    allocation = allocate_instrument(
        signal,
        _underlying_quote(),
        [],
        _account_state(),
        load_runtime_config(paper_root),
        REGULAR_NOW,
        planned_exit_at=NEXT_CLOSE_EXIT,
    )

    assert allocation["status"] == "no_trade"
    assert allocation["selected_instrument"] is None
    assert allocation["considered"] == []
    assert allocation["reason"].startswith("invalid actionable signal:")


def test_allocator_execution_rejects_invalid_persisted_signal_before_market_data(
    paper_root: Path,
) -> None:
    from scripts.discovery.ai_instrument_allocator_pipeline import (
        AiInstrumentAllocatorPipeline,
    )

    class MustNotFetchExecutionData(_AllocatorExecutionDiscovery):
        def fetch_current_quote(self, *_args, **_kwargs):
            raise AssertionError("invalid persisted signal must fail before market data")

    config = load_runtime_config(paper_root)
    tracker = UsageTracker()
    pipeline = AiInstrumentAllocatorPipeline(
        paper_root,
        config,
        MockProvider(tracker),
        tracker,
        discovery_adapter=MustNotFetchExecutionData(),
        news_adapter=_NoNews(),
        option_data=_NoOptions(),
    )
    invalid_signal = _anchored_signal()
    invalid_signal["max_holding_trading_days"] = None
    result = pipeline._execute_plan(
        {
            "plan_id": "invalid-persisted-plan",
            "strategy": "ai_instrument_allocator_v1",
            "ticker": "AAPL",
            "stage": "intraday",
            "signal": invalid_signal,
            "snapshot": {"snapshot_id": "invalid-persisted-snapshot"},
        },
        REGULAR_NOW,
        stage="intraday",
    )

    assert result["status"] == "no_trade"
    assert result["order"] is None
    assert result["reason"].startswith("invalid actionable signal:")


def test_allocator_execution_rejects_null_persisted_signal_before_market_data(
    paper_root: Path,
) -> None:
    from scripts.discovery.ai_instrument_allocator_pipeline import (
        AiInstrumentAllocatorPipeline,
    )

    class MustNotFetchExecutionData(_AllocatorExecutionDiscovery):
        def fetch_current_quote(self, *_args, **_kwargs):
            raise AssertionError("null persisted signal must fail before market data")

    config = load_runtime_config(paper_root)
    tracker = UsageTracker()
    pipeline = AiInstrumentAllocatorPipeline(
        paper_root,
        config,
        MockProvider(tracker),
        tracker,
        discovery_adapter=MustNotFetchExecutionData(),
        news_adapter=_NoNews(),
        option_data=_AllocatorNoOptions(),
    )

    result = pipeline._execute_plan(
        {
            "plan_id": "null-signal-plan",
            "strategy": "ai_instrument_allocator_v1",
            "ticker": "AAPL",
            "stage": "intraday",
            "signal": None,
            "snapshot": {"snapshot_id": "null-signal-snapshot"},
        },
        REGULAR_NOW,
        stage="intraday",
    )

    assert result["status"] == "no_trade"
    assert result["order"] is None
    assert result["reason"].startswith("invalid actionable signal:")


def test_allocator_execution_rejects_invalid_quote_timestamp(
    paper_root: Path,
) -> None:
    from scripts.discovery.ai_instrument_allocator_pipeline import (
        AiInstrumentAllocatorPipeline,
    )

    config = load_runtime_config(paper_root)
    tracker = UsageTracker()
    pipeline = AiInstrumentAllocatorPipeline(
        paper_root,
        config,
        MockProvider(tracker),
        tracker,
        discovery_adapter=_AllocatorExecutionDiscovery("not-a-time"),
        news_adapter=_NoNews(),
        option_data=_AllocatorNoOptions(),
    )

    result = pipeline._execute_plan(
        {
            "plan_id": "invalid-quote-time-plan",
            "strategy": "ai_instrument_allocator_v1",
            "ticker": "AAPL",
            "stage": "intraday",
            "signal": _anchored_signal(),
            "snapshot": {"snapshot_id": "invalid-quote-time-snapshot"},
        },
        REGULAR_NOW,
        stage="intraday",
    )

    assert result["status"] == "no_trade"
    assert result["order"] is None
    assert result["reason"].startswith("fresh executable data failed closed:")


@pytest.mark.parametrize(
    ("now", "expected_exit_date", "minimum_elapsed_days"),
    [
        ("2026-07-17T15:00:00+00:00", "2026-07-20", 3.0),
        ("2026-09-04T15:00:00+00:00", "2026-09-08", 4.0),
    ],
)
def test_allocator_option_repricing_uses_calendar_elapsed_time_to_next_session(
    paper_root: Path,
    now: str,
    expected_exit_date: str,
    minimum_elapsed_days: float,
) -> None:
    from scripts.core.models import Quote, parse_ts
    from scripts.decision.instrument_allocator import allocate_instrument
    from scripts.exit.position_mandates import planned_exit_time

    planned_exit_at = planned_exit_time(
        now,
        "next_close",
        max_holding_trading_days=1,
        minutes_before_close=10,
    )
    signal = _anchored_signal(
        forecast_reference_time=now,
        thesis_valid_until=planned_exit_at,
    )
    quote = Quote(
        "AAPL",
        bid=100.0,
        ask=100.05,
        last=100.02,
        asof=now,
        source="fixture",
        avg_daily_volume_usd=500_000_000,
    )
    allocation = allocate_instrument(
        signal,
        quote,
        [
            (
                _option_contract("aapl-calendar-call", "2026-10-16"),
                replace(_option_quote("aapl-calendar-call"), updated_at=now),
            )
        ],
        _account_state(),
        load_runtime_config(paper_root),
        now,
        planned_exit_at=planned_exit_at,
    )

    elapsed = (parse_ts(planned_exit_at) - parse_ts(now)).total_seconds() / 86_400
    option = next(
        item for item in allocation["considered"] if item["instrument_type"] == "call"
    )
    assert parse_ts(planned_exit_at).date().isoformat() == expected_exit_date
    assert elapsed > minimum_elapsed_days
    assert allocation["scenario_elapsed_calendar_days"] == pytest.approx(elapsed)
    assert option["scenario_repricing"]["elapsed_calendar_days"] == pytest.approx(elapsed)


def test_allocator_uses_put_only_for_bearish_executable_direction(
    paper_root: Path,
) -> None:
    from scripts.decision.instrument_allocator import allocate_instrument

    config = load_runtime_config(paper_root)
    put = _option_contract("aapl-put", "2026-08-21", "put")
    quote = _option_quote("aapl-put", bid=4.95, ask=5.05)
    allocation = allocate_instrument(
        {
            **_bearish_signal(),
            "forecast_reference_price": 100.0,
            "forecast_reference_time": REGULAR_NOW,
        },
        _underlying_quote(),
        [(put, quote)],
        _account_state(),
        config,
        REGULAR_NOW,
        planned_exit_at=NEXT_CLOSE_EXIT,
    )

    considered = {item["instrument_type"] for item in allocation["considered"]}
    assert considered == {"put"}
    assert allocation["considered"][0]["break_even_move_pct"] < 0
    assert allocation["short_equity_counterfactual"]["benchmark_name"] == "short_equity_counterfactual"
    assert allocation["short_equity_counterfactual"]["creates_order"] is False
    assert "account" not in allocation["short_equity_counterfactual"]


def test_allocator_uses_remaining_move_from_fixed_forecast_reference(
    paper_root: Path,
) -> None:
    from scripts.core.models import Quote
    from scripts.decision.instrument_allocator import allocate_instrument

    quote = Quote(
        "AAPL",
        bid=101.99,
        ask=102.01,
        last=102.0,
        asof=REGULAR_NOW,
        source="fixture",
        avg_daily_volume_usd=500_000_000,
    )
    allocation = allocate_instrument(
        _anchored_signal(),
        quote,
        [],
        _account_state(),
        load_runtime_config(paper_root),
        REGULAR_NOW,
        planned_exit_at=NEXT_CLOSE_EXIT,
    )

    assert allocation["forecast_reference_price"] == 100.0
    assert allocation["forecast_target_price"] == pytest.approx(100.7142857143)
    assert allocation["realized_move_since_reference_pct"] == pytest.approx(2.0)
    assert allocation["remaining_move_pct"] == pytest.approx(-1.2605042017)
    assert allocation["status"] == "no_trade"


def test_allocator_fails_closed_without_forecast_reference(
    paper_root: Path,
) -> None:
    from scripts.decision.instrument_allocator import allocate_instrument

    allocation = allocate_instrument(
        _signed_signal(),
        _underlying_quote(),
        [],
        _account_state(),
        load_runtime_config(paper_root),
        REGULAR_NOW,
        planned_exit_at=NEXT_CLOSE_EXIT,
    )

    assert allocation["status"] == "no_trade"
    assert allocation["reason"] == "missing or invalid forecast reference"
    assert allocation["considered"] == []


def test_allocator_records_market_implied_move_comparison(
    paper_root: Path,
) -> None:
    import math

    from scripts.core.models import parse_ts
    from scripts.decision.instrument_allocator import allocate_instrument

    allocation = allocate_instrument(
        _anchored_signal(),
        _underlying_quote(),
        [(_option_contract("aapl-call", "2026-08-21"), _option_quote("aapl-call"))],
        _account_state(),
        load_runtime_config(paper_root),
        REGULAR_NOW,
        planned_exit_at=NEXT_CLOSE_EXIT,
    )

    elapsed_days = (
        parse_ts(NEXT_CLOSE_EXIT) - parse_ts(REGULAR_NOW)
    ).total_seconds() / 86_400
    implied = 0.30 * math.sqrt(elapsed_days / 365) * 100
    assert allocation["market_implied_move_pct"] == pytest.approx(implied)
    assert allocation["forecast_to_implied_move_ratio"] == pytest.approx(
        abs(allocation["remaining_move_pct"]) / implied
    )
    assert allocation["market_implied_move_method"] == (
        "nearest_candidate_iv_sqrt_calendar_time"
    )


def test_instrument_allocation_schema_accepts_forecast_and_implied_move_audit(
    paper_root: Path,
) -> None:
    from jsonschema import Draft202012Validator, FormatChecker

    from scripts.decision.instrument_allocator import allocate_instrument

    allocation = allocate_instrument(
        _anchored_signal(),
        _underlying_quote(),
        [(_option_contract("aapl-call", "2026-08-21"), _option_quote("aapl-call"))],
        _account_state(),
        load_runtime_config(paper_root),
        REGULAR_NOW,
        planned_exit_at=NEXT_CLOSE_EXIT,
    )
    schema = json.loads(
        (Path(__file__).resolve().parents[1] / "schemas" / "instrument_allocation.schema.json").read_text(
            encoding="utf-8"
        )
    )

    Draft202012Validator(schema, format_checker=FormatChecker()).validate(allocation)


def test_allocator_rejects_neutral_signal_before_instrument_comparison(
    paper_root: Path,
) -> None:
    from scripts.decision.instrument_allocator import allocate_instrument

    allocation = allocate_instrument(
        {
            **_neutral_signal(),
            "forecast_reference_price": 100.0,
            "forecast_reference_time": REGULAR_NOW,
        },
        _underlying_quote(),
        [],
        _account_state(),
        load_runtime_config(paper_root),
        REGULAR_NOW,
        planned_exit_at=NEXT_CLOSE_EXIT,
    )

    assert allocation["status"] == "no_trade"
    assert allocation["reason"] == "signed return direction is neutral or insufficiently dominant"
    assert allocation["considered"] == []


def test_two_thousand_counterfactual_never_reselects_instrument() -> None:
    from scripts.decision.instrument_allocator import build_same_instrument_counterfactual

    selected = {
        "allocation_id": "allocation-put",
        "selected_instrument": {
            "instrument_type": "put",
            "ticker": "AAPL",
            "option_id": "aapl-put",
            "expiration_date": "2026-08-21",
            "strike_price": 100.0,
            "entry_price": 2.50,
            "multiplier": 100,
            "quantity": 1,
            "risk_usd": 250.0,
        },
    }

    counterfactual = build_same_instrument_counterfactual(selected, nav_usd=2_000)

    assert counterfactual["source_allocation_id"] == "allocation-put"
    assert counterfactual["instrument_identity"]["option_id"] == "aapl-put"
    assert counterfactual["affordable"] is False
    assert counterfactual["max_affordable_quantity"] == 0
    assert counterfactual["risk_pct_of_nav"] == 0.125
    assert counterfactual["rejection_reason"] == "same option contract exceeds 3% per-entry premium risk"
    assert counterfactual["alternative_instrument_considered"] is False


def test_two_thousand_equity_counterfactual_reports_scaled_position_risk() -> None:
    from scripts.decision.instrument_allocator import build_same_instrument_counterfactual

    selected = {
        "allocation_id": "allocation-equity",
        "selected_instrument": {
            "instrument_type": "equity",
            "ticker": "AAPL",
            "entry_price": 100.0,
            "planned_stop_price": 97.0,
            "quantity": 25.0,
            "risk_usd": 75.0,
        },
    }

    counterfactual = build_same_instrument_counterfactual(selected, nav_usd=2_000)

    assert counterfactual["affordable"] is True
    assert counterfactual["max_affordable_quantity"] == 5.0
    assert counterfactual["proposed_risk_usd"] == 15.0
    assert counterfactual["risk_pct_of_nav"] == 0.0075
    assert counterfactual["alternative_instrument_considered"] is False


def test_allocator_risk_configuration_has_effective_portfolio_caps(
    paper_root: Path,
) -> None:
    config = load_runtime_config(paper_root)

    assert config["risk"]["max_position_pct_of_equity"] == 0.25
    assert config["risk"]["max_planned_loss_pct_of_equity"] == 0.01
    assert config["options_risk"]["max_order_risk_pct_of_equity"] == 0.03
    assert config["options_risk"]["max_line_deployed_pct_of_equity"] == 0.08
    assert config["shared_risk"]["max_options_deployed_pct_of_equity"] == 0.08
    assert config["shared_risk"]["max_total_open_positions"] == 3
    assert config["shared_risk"]["max_total_daily_entry_trades"] == 3
    assert config["shared_risk"]["one_exposure_per_underlying"] is True


def test_allocator_equity_order_requires_stop_and_caps_planned_nav_loss(
    paper_root: Path,
) -> None:
    from scripts.core.models import Account, Order
    from scripts.risk.risk_gate import check_order

    config = load_runtime_config(paper_root)
    quote = _underlying_quote()

    def decision(stop_price):
        order = Order(
            order_id=f"equity-{stop_price}",
            decision_id="allocator-equity",
            symbol="AAPL",
            side="buy",
            order_type="limit",
            quantity=20,
            limit_price=100.06,
            quote_seen_at=REGULAR_NOW,
            created_at=REGULAR_NOW,
            strategy="ai_instrument_allocator_v1",
            planned_stop_price=stop_price,
            signal_horizon="next_close",
        )
        return check_order(
            order,
            quote,
            Account(10_000, 10_000),
            {},
            {},
            {"trades": 0},
            config,
            REGULAR_NOW,
            option_positions={},
            option_orders={},
        )

    assert decision(None).reason == "allocator equity entry requires a planned stop"
    assert decision(94.0).reason == "planned stop NAV risk exceeded"
    assert decision(97.0).approved is True


def test_single_underlying_and_three_position_caps_apply_across_lines(
    paper_root: Path,
) -> None:
    from scripts.core.models import Account, Order, Position
    from scripts.options.models import OptionOrder, OptionPosition
    from scripts.options.risk_gate import check_option_order
    from scripts.risk.risk_gate import check_order

    config = load_runtime_config(paper_root)
    account = Account(10_000, 10_000)
    option_contract = _option_contract("aapl-put-risk", "2026-08-21", "put")
    option_quote = _option_quote("aapl-put-risk", bid=1.99, ask=2.0)
    option_order = OptionOrder(
        order_id="cross-line-option",
        decision_id="cross-line-option",
        contract=option_contract,
        intent="buy_to_open",
        quantity=1,
        order_type="limit",
        limit_price=2.01,
        quote_seen_at=REGULAR_NOW,
        created_at=REGULAR_NOW,
        strategy="ai_instrument_allocator_v1",
        signal_horizon="next_close",
    )
    equity_positions = {
        "AAPL": Position("AAPL", 1, 100, REGULAR_NOW, REGULAR_NOW)
    }
    option_decision = check_option_order(
        option_order,
        option_quote,
        account,
        equity_positions,
        {},
        {},
        {},
        {"trades": 0},
        config,
        REGULAR_NOW,
    )
    assert option_decision.reason == "underlying already has executable equity exposure"

    existing_option = OptionPosition(
        option_contract,
        1,
        1.0,
        REGULAR_NOW,
        REGULAR_NOW,
    )
    equity_order = Order(
        order_id="cross-line-equity",
        decision_id="cross-line-equity",
        symbol="AAPL",
        side="buy",
        order_type="limit",
        quantity=1,
        limit_price=100.06,
        quote_seen_at=REGULAR_NOW,
        created_at=REGULAR_NOW,
        strategy="ai_instrument_allocator_v1",
        planned_stop_price=97.0,
        signal_horizon="next_close",
    )
    equity_decision = check_order(
        equity_order,
        _underlying_quote(),
        account,
        {},
        {},
        {"trades": 0},
        config,
        REGULAR_NOW,
        option_positions={option_contract.option_id: existing_option},
        option_orders={},
    )
    assert equity_decision.reason == "underlying already has executable option exposure"

    three_positions = {
        "MSFT": Position("MSFT", 1, 100, REGULAR_NOW, REGULAR_NOW),
        "NVDA": Position("NVDA", 1, 100, REGULAR_NOW, REGULAR_NOW),
        "GOOGL": Position("GOOGL", 1, 100, REGULAR_NOW, REGULAR_NOW),
    }
    full_decision = check_order(
        Order(
            order_id="fourth-position",
            decision_id="fourth-position",
            symbol="AAPL",
            side="buy",
            order_type="limit",
            quantity=1,
            limit_price=100.06,
            quote_seen_at=REGULAR_NOW,
            created_at=REGULAR_NOW,
            strategy="ai_instrument_allocator_v1",
            planned_stop_price=97.0,
            signal_horizon="next_close",
        ),
        _underlying_quote(),
        account,
        three_positions,
        {},
        {"trades": 0},
        config,
        REGULAR_NOW,
        option_positions={},
        option_orders={},
    )
    assert full_decision.reason == "shared max total open positions reached"


def test_option_aggregate_eight_percent_cap_rejects_third_risk_block(
    paper_root: Path,
) -> None:
    from scripts.core.models import Account
    from scripts.options.models import OptionOrder, OptionPosition
    from scripts.options.risk_gate import check_option_order

    config = load_runtime_config(paper_root)
    existing_contract = replace(
        _option_contract("existing-put", "2026-08-21", "put"),
        underlying="MSFT",
    )
    existing = OptionPosition(
        existing_contract,
        1,
        7.0,
        REGULAR_NOW,
        REGULAR_NOW,
    )
    new_contract = _option_contract("new-put", "2026-08-21", "put")
    order = OptionOrder(
        order_id="aggregate-option",
        decision_id="aggregate-option",
        contract=new_contract,
        intent="buy_to_open",
        quantity=1,
        order_type="limit",
        limit_price=2.01,
        quote_seen_at=REGULAR_NOW,
        created_at=REGULAR_NOW,
        strategy="ai_instrument_allocator_v1",
        signal_horizon="next_close",
    )
    decision = check_option_order(
        order,
        _option_quote("new-put", bid=1.99, ask=2.0),
        Account(9_300, 10_000),
        {},
        {existing_contract.option_id: existing},
        {},
        {},
        {"trades": 0},
        config,
        REGULAR_NOW,
    )

    assert decision.reason == "shared options deployed risk cap exceeded"


def test_allocator_plan_and_position_mandate_survive_restart(
    paper_root: Path,
) -> None:
    from scripts.exit.position_mandates import PositionMandateStore
    from scripts.strategies.allocator_state import AllocatorStateStore

    namespace = "ai_instrument_allocator_v1"
    legacy_account_before = (paper_root / "state" / "paper_account.json").read_bytes()
    plans = AllocatorStateStore(paper_root, namespace=namespace)
    plan = {
        "plan_id": "plan-aapl",
        "strategy": namespace,
        "ticker": "AAPL",
        "created_at": REGULAR_NOW,
        "valid_until": "2026-07-13T15:05:00+00:00",
        "status": "active",
        "signal": _signed_signal(),
        "snapshot": {"snapshot_id": "snapshot-aapl"},
    }
    plans.save_plan(plan)
    plans.record_allocation(
        {
            "allocation_id": "allocation-aapl",
            "plan_id": "plan-aapl",
            "decision_time": REGULAR_NOW,
            "status": "selected",
        }
    )

    mandates = PositionMandateStore(paper_root, namespace=namespace)
    mandates.register_order(
        order_id="paper-order-aapl",
        exposure_id="equity:AAPL",
        strategy=namespace,
        snapshot_id="snapshot-aapl",
        ticker="AAPL",
        instrument_type="equity",
        horizon="next_close",
        max_holding_trading_days=1,
        created_at=REGULAR_NOW,
        planned_exit_at="2026-07-14T19:50:00+00:00",
        thesis_valid_until="2026-07-14T19:50:00+00:00",
        invalidation_condition="Close below 97.",
        planned_stop_price=97.0,
    )

    restarted_plans = AllocatorStateStore(paper_root, namespace=namespace)
    restarted_mandates = PositionMandateStore(paper_root, namespace=namespace)
    assert restarted_plans.active_plans(REGULAR_NOW)[0]["plan_id"] == "plan-aapl"
    assert restarted_plans.allocations()["allocation-aapl"]["plan_id"] == "plan-aapl"
    persisted = restarted_mandates.for_exposure("equity:AAPL")
    assert persisted["mandate_version"] == 2
    assert persisted["horizon"] == "next_close"
    assert persisted["max_holding_trading_days"] == 1

    from jsonschema import Draft202012Validator, FormatChecker, ValidationError

    schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "schemas"
            / "position_mandate.schema.json"
        ).read_text(encoding="utf-8")
    )
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    validator.validate(persisted)
    legacy = dict(persisted)
    legacy["mandate_version"] = 1
    legacy.pop("max_holding_trading_days")
    validator.validate(legacy)
    inconsistent = {**persisted, "max_holding_trading_days": 5}
    with pytest.raises(ValidationError):
        validator.validate(inconsistent)
    assert (paper_root / "state" / "paper_account.json").read_bytes() == legacy_account_before


def test_allocator_state_skips_corrupt_null_records(paper_root: Path) -> None:
    from scripts.strategies.allocator_state import AllocatorStateStore

    state = AllocatorStateStore(paper_root)
    state.store.write_json("allocator_plans.json", {"corrupt": None})
    state.store.write_json("allocator_allocations.json", {"corrupt": None})

    assert state.plans() == {}
    assert state.active_plans(REGULAR_NOW) == []
    assert state.allocations() == {}


def test_position_mandate_exit_is_horizon_aware_and_fails_closed() -> None:
    from scripts.exit.position_mandates import evaluate_mandate_exit

    mandate = {
        "mandate_version": 2,
        "exposure_id": "equity:AAPL",
        "order_id": "paper-order-aapl",
        "strategy": "ai_instrument_allocator_v1",
        "snapshot_id": "snapshot-aapl",
        "status": "open",
        "ticker": "AAPL",
        "instrument_type": "equity",
        "horizon": "next_close",
        "max_holding_trading_days": 1,
        "created_at": REGULAR_NOW,
        "entered_at": REGULAR_NOW,
        "planned_exit_at": "2026-07-14T19:50:00+00:00",
        "thesis_valid_until": "2026-07-14T19:50:00+00:00",
        "invalidation_condition": "Close below 97.",
        "invalidation_triggered": False,
        "planned_stop_price": 97.0,
        "closed_at": None,
        "close_reason": None,
    }

    assert evaluate_mandate_exit(mandate, "2026-07-14T18:00:00+00:00").should_exit is False
    expired = evaluate_mandate_exit(mandate, "2026-07-14T19:51:00+00:00")
    assert expired.should_exit is True
    assert expired.reason == "position mandate planned exit reached"
    missing = evaluate_mandate_exit(None, "2026-07-14T18:00:00+00:00")
    assert missing.should_exit is True
    assert missing.reason == "missing position mandate; fail closed"


class _AllocatorExecutionDiscovery:
    def __init__(self, quote_asof=REGULAR_NOW):
        self.quote_asof = quote_asof

    def collect_seed_candidates(self, _now, _watchlist):
        return []

    def fetch_market_context(self, _tickers, _now):
        return {}

    def fetch_current_quote(self, symbol: str, **_kwargs):
        from scripts.core.models import Quote

        return Quote(
            symbol,
            100.00,
            100.01,
            100.005,
            self.quote_asof,
            source="allocator-test",
            avg_daily_volume_usd=500_000_000,
        )


class _AllocatorResearchDiscovery(_AllocatorExecutionDiscovery):
    def collect_seed_candidates(self, _now, _watchlist):
        return [{"ticker": "AAPL", "sources": ["fixture_scan"]}]

    def fetch_market_context(self, _tickers, decision_time):
        return {
            "AAPL": {
                "ticker": "AAPL",
                "eligible": True,
                "quote": {
                    "symbol": "AAPL",
                    "bid": 100.00,
                    "ask": 100.01,
                    "last": 100.005,
                    "asof": decision_time,
                    "source": "allocator-test",
                    "avg_daily_volume_usd": 500_000_000,
                    "asset_class": "us_equity",
                    "is_otc": False,
                    "is_leveraged_etf": False,
                    "is_inverse_etf": False,
                    "halted": False,
                    "session_volume": 2_000_000,
                    "previous_close": 98.00,
                },
                "fundamentals": {"market_cap": 3_000_000_000_000},
                "technical_signals": {
                    "price_change_1d_pct": 2.0,
                    "price_change_5d_pct": 6.0,
                    "relative_strength_20d": 5.0,
                    "volume_ratio": 1.5,
                },
            }
        }

    @staticmethod
    def validate_instrument(symbol):
        return {"valid": True, "name": "Apple Inc.", "symbol": symbol}


class _AllocatorResearchNews:
    def __init__(self, direction="positive", *, already_priced_in=False):
        self.direction = direction
        self.already_priced_in = already_priced_in

    def search(self, ticker, decision_time, company_name=None):
        del company_name
        positive = self.direction == "positive"
        return [
            {
                "ticker": ticker,
                "headline": (
                    "Company raises full-year guidance"
                    if positive
                    else "Company withdraws full-year guidance"
                ),
                "published_at": (
                    "2026-07-12T23:30:00+00:00"
                    if positive
                    else "2026-07-13T11:30:00+00:00"
                ),
                "event_at": (
                    "2026-07-12T23:25:00+00:00"
                    if positive
                    else "2026-07-13T11:25:00+00:00"
                ),
                "first_seen_at": (
                    "2026-07-12T23:31:00+00:00"
                    if positive
                    else "2026-07-13T11:31:00+00:00"
                ),
                "retrieved_at": decision_time,
                "source": "company.example",
                "source_tier": 1,
                "ticker_relevance": 1.0,
                "direction": self.direction,
                "novelty": 0.95,
                "already_priced_in": self.already_priced_in,
                "confidence": 0.9,
                "url": (
                    "https://company.example/investors/guidance"
                    if positive
                    else "https://company.example/investors/withdrawn-guidance"
                ),
                "highlights": [
                    "Full-year revenue guidance increased."
                    if positive
                    else "Full-year revenue guidance was withdrawn."
                ],
            }
        ], [
            {
                "source": "company.example",
                "source_tier": 1,
                "retrieved_at": decision_time,
            }
        ]


class _AllocatorNoOptions:
    def fetch_contract_candidates(self, **_kwargs):
        return [], {"candidate_count": 0}

    def fetch_quotes(self, _option_ids):
        return {}


class _AllocatorFutureOptionData(_AllocatorNoOptions):
    def fetch_contract_candidates(self, **_kwargs):
        contract = _option_contract("aapl-put-future", "2026-08-21", "put")
        quote = replace(
            _option_quote("aapl-put-future", bid=1.95, ask=1.96),
            updated_at="2026-07-13T13:32:05+00:00",
        )
        return [(contract, quote)], {"candidate_count": 1}


def test_open_execution_uses_saved_conditional_plan_without_llm_and_only_new_namespace(
    paper_root: Path,
) -> None:
    from scripts.discovery.ai_instrument_allocator_pipeline import (
        AiInstrumentAllocatorPipeline,
    )

    config = load_runtime_config(paper_root)
    legacy_account_before = (paper_root / "state" / "paper_account.json").read_bytes()
    tracker = UsageTracker()
    pipeline = AiInstrumentAllocatorPipeline(
        paper_root,
        config,
        MockProvider(tracker),
        tracker,
        discovery_adapter=_AllocatorExecutionDiscovery(OPEN_EXECUTION_NOW),
        news_adapter=_NoNews(),
        option_data=_AllocatorNoOptions(),
    )
    pipeline.plans.save_plan(
        {
            "plan_id": "open-plan-aapl",
            "strategy": "ai_instrument_allocator_v1",
            "ticker": "AAPL",
            "created_at": "2026-07-13T13:27:00+00:00",
            "valid_until": "2026-07-13T13:37:00+00:00",
            "status": "active",
            "stage": "overnight",
            "preopen_revalidated_at": "2026-07-13T13:25:00+00:00",
            "signal": _anchored_signal(
                entry_now=False,
                forecast_reference_time="2026-07-13T13:27:00+00:00",
            ),
            "snapshot": {"snapshot_id": "allocator-open-snapshot"},
        }
    )

    result = pipeline.run_stage("open_execution", OPEN_EXECUTION_NOW)

    assert result["event"] == "ai_instrument_allocator_stage_complete"
    assert result["model_calls"] == 0
    assert result["paper_orders_created"] == 1, result
    assert result["executions"][0]["order"]["status"] == "filled"
    assert result["live_order_tools_called"] is False
    assert pipeline.broker.store.account().initial_cash == 10_000
    assert set(pipeline.broker.store.positions()) == {"AAPL"}
    assert pipeline.mandates.for_exposure("equity:AAPL")["status"] == "open"
    assert (paper_root / "state" / "paper_account.json").read_bytes() == legacy_account_before


def test_open_execution_requires_current_preopen_revalidation(
    paper_root: Path,
) -> None:
    from scripts.discovery.ai_instrument_allocator_pipeline import (
        AiInstrumentAllocatorPipeline,
    )

    config = load_runtime_config(paper_root)
    tracker = UsageTracker()
    pipeline = AiInstrumentAllocatorPipeline(
        paper_root,
        config,
        MockProvider(tracker),
        tracker,
        discovery_adapter=_AllocatorExecutionDiscovery(OPEN_EXECUTION_NOW),
        news_adapter=_NoNews(),
        option_data=_AllocatorNoOptions(),
    )
    pipeline.plans.save_plan(
        {
            "plan_id": "unvalidated-open-plan",
            "strategy": "ai_instrument_allocator_v1",
            "ticker": "AAPL",
            "created_at": "2026-07-13T00:00:00+00:00",
            "valid_until": "2026-07-13T13:37:00+00:00",
            "status": "active",
            "stage": "overnight",
            "signal": _signed_signal(entry_now=False),
            "snapshot": {"snapshot_id": "unvalidated-open-snapshot"},
        }
    )

    result = pipeline.run_stage("open_execution", OPEN_EXECUTION_NOW)

    assert result["paper_orders_created"] == 0
    assert result["executions"][0]["reason"] == "current pre-open revalidation is required"
    assert pipeline.broker.store.orders() == {}


def test_allocator_mock_dry_run_survives_overnight_open_and_restart(
    paper_root: Path,
) -> None:
    from scripts.discovery.ai_instrument_allocator_pipeline import (
        AiInstrumentAllocatorPipeline,
    )

    config = load_runtime_config(paper_root)
    tracker = UsageTracker()
    pipeline = AiInstrumentAllocatorPipeline(
        paper_root,
        config,
        MockProvider(tracker),
        tracker,
        discovery_adapter=_AllocatorResearchDiscovery(OPEN_EXECUTION_NOW),
        news_adapter=_AllocatorResearchNews(),
        option_data=_AllocatorNoOptions(),
    )

    overnight = pipeline.run_stage("overnight", "2026-07-13T00:00:00+00:00")

    assert overnight["model_calls"] == 4
    assert overnight["paper_orders_created"] == 0
    assert len(overnight["plans"]) == 1
    assert overnight["plans"][0]["signal"]["entry_now"] is False
    assert pipeline.broker.store.orders() == {}
    assert pipeline.option_broker.store.orders() == {}

    premarket = pipeline.run_stage(
        "premarket_update",
        "2026-07-13T12:00:00+00:00",
    )
    preopen = pipeline.run_stage(
        "preopen_revalidation",
        "2026-07-13T13:25:00+00:00",
    )

    assert premarket["paper_orders_created"] == 0
    assert premarket["model_calls"] == 0
    assert preopen["paper_orders_created"] == 0
    assert preopen["model_calls"] == 0
    assert len(pipeline.plans.active_plans(OPEN_EXECUTION_NOW)) == 1
    assert pipeline.plans.active_plans(OPEN_EXECUTION_NOW)[0][
        "preopen_revalidated_at"
    ] == "2026-07-13T13:25:00+00:00"

    opened = pipeline.run_stage("open_execution", OPEN_EXECUTION_NOW)

    assert opened["model_calls"] == 0
    assert opened["paper_orders_created"] == 1
    assert opened["executions"][0]["status"] == "filled"
    assert opened["live_order_tools_called"] is False

    restart_tracker = UsageTracker()
    restarted = AiInstrumentAllocatorPipeline(
        paper_root,
        config,
        MockProvider(restart_tracker),
        restart_tracker,
        discovery_adapter=_AllocatorExecutionDiscovery(OPEN_EXECUTION_NOW),
        news_adapter=_NoNews(),
        option_data=_AllocatorNoOptions(),
    )
    monitored = restarted.monitor_only(OPEN_EXECUTION_NOW)

    assert set(restarted.broker.store.positions()) == {"AAPL"}
    assert restarted.mandates.for_exposure("equity:AAPL")["status"] == "open"
    assert monitored["event"] == "ai_instrument_allocator_monitor_complete"
    assert monitored["live_order_tools_called"] is False


def test_open_execution_rejects_outside_configured_open_window(
    paper_root: Path,
) -> None:
    from scripts.discovery.ai_instrument_allocator_pipeline import (
        AiInstrumentAllocatorPipeline,
    )

    config = load_runtime_config(paper_root)
    tracker = UsageTracker()
    pipeline = AiInstrumentAllocatorPipeline(
        paper_root,
        config,
        MockProvider(tracker),
        tracker,
        discovery_adapter=_AllocatorExecutionDiscovery(),
        news_adapter=_NoNews(),
        option_data=_AllocatorNoOptions(),
    )
    pipeline.plans.save_plan(
        {
            "plan_id": "late-open-plan",
            "strategy": "ai_instrument_allocator_v1",
            "ticker": "AAPL",
            "created_at": "2026-07-13T13:00:00+00:00",
            "valid_until": "2026-07-13T16:00:00+00:00",
            "status": "active",
            "stage": "overnight",
            "signal": _signed_signal(entry_now=False),
            "snapshot": {"snapshot_id": "late-open-snapshot"},
        }
    )

    result = pipeline.run_stage("open_execution", REGULAR_NOW)

    assert result["paper_orders_created"] == 0
    assert result["executions"] == []
    assert result["reason"] == "stage is outside its market window: regular"


def test_open_execution_rejects_intraday_plan_source(
    paper_root: Path,
) -> None:
    from scripts.discovery.ai_instrument_allocator_pipeline import (
        AiInstrumentAllocatorPipeline,
    )

    config = load_runtime_config(paper_root)
    tracker = UsageTracker()
    pipeline = AiInstrumentAllocatorPipeline(
        paper_root,
        config,
        MockProvider(tracker),
        tracker,
        discovery_adapter=_AllocatorExecutionDiscovery(OPEN_EXECUTION_NOW),
        news_adapter=_NoNews(),
        option_data=_AllocatorNoOptions(),
    )
    pipeline.plans.save_plan(
        {
            "plan_id": "intraday-carry-plan",
            "strategy": "ai_instrument_allocator_v1",
            "ticker": "AAPL",
            "created_at": "2026-07-13T13:30:00+00:00",
            "valid_until": "2026-07-13T13:37:00+00:00",
            "status": "active",
            "stage": "intraday",
            "signal": _signed_signal(entry_now=True),
            "snapshot": {"snapshot_id": "intraday-carry-snapshot"},
        }
    )

    result = pipeline.run_stage("open_execution", OPEN_EXECUTION_NOW)

    assert result["paper_orders_created"] == 0
    assert result["executions"][0]["reason"] == "plan source is not eligible for open execution"


def test_premarket_research_replaces_older_plan_for_same_ticker(
    paper_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.decision.signed_return_signal import derive_signal_summary
    from scripts.discovery.ai_instrument_allocator_pipeline import (
        AiInstrumentAllocatorPipeline,
    )

    class RecordingMockProvider(MockProvider):
        def __init__(self, tracker):
            super().__init__(tracker)
            self.requests = []

        def generate(self, request):
            self.requests.append(request)
            return super().generate(request)

    config = load_runtime_config(paper_root)
    tracker = UsageTracker()
    provider = RecordingMockProvider(tracker)
    news = _AllocatorResearchNews()
    pipeline = AiInstrumentAllocatorPipeline(
        paper_root,
        config,
        provider,
        tracker,
        discovery_adapter=_AllocatorResearchDiscovery(),
        news_adapter=news,
        option_data=_AllocatorNoOptions(),
    )

    overnight = pipeline.run_stage("overnight", "2026-07-13T00:00:00+00:00")
    old_plan_id = overnight["plans"][0]["plan_id"]
    old_signal = dict(overnight["plans"][0]["signal"])
    news.direction = "negative"
    pipeline.discovery = _MustNotDiscover()
    calls_before = len(tracker.records)
    requests_before = len(provider.requests)
    plan_writes = 0
    write_json = pipeline.plans.store.write_json

    def count_plan_writes(name, value):
        nonlocal plan_writes
        if name == "allocator_plans.json":
            plan_writes += 1
        return write_json(name, value)

    monkeypatch.setattr(pipeline.plans.store, "write_json", count_plan_writes)

    updated = pipeline.run_stage(
        "premarket_update",
        "2026-07-13T12:00:00+00:00",
    )

    active = pipeline.plans.active_plans(REGULAR_NOW)
    assert updated["model_calls"] == 3
    assert [
        record.agent_name for record in tracker.records[calls_before:]
    ] == [
        "ai_allocator_fast_news_agent",
        "ai_allocator_fast_challenge_agent",
        "ai_allocator_fast_decision_manager",
    ]
    assert updated["paper_orders_created"] == 0
    incremental_requests = provider.requests[requests_before:]
    assert all(
        [event["headline"] for event in request.input_payload["available_news"]]
        == ["Company withdraws full-year guidance"]
        for request in incremental_requests
    )
    assert all(
        request.input_payload["agent_context"]["prior_signal"] == old_signal
        and request.input_payload["agent_context"]["incremental_update"] is True
        for request in incremental_requests
    )
    assert plan_writes == 1
    assert len(active) == 1
    assert active[0]["plan_id"] != old_plan_id
    assert derive_signal_summary(active[0]["signal"])["direction"] == "bearish"
    assert active[0]["signal"]["forecast_reference_price"] == old_signal[
        "forecast_reference_price"
    ]
    assert active[0]["signal"]["forecast_reference_time"] == old_signal[
        "forecast_reference_time"
    ]
    assert pipeline.plans.plans()[old_plan_id]["status"] == "superseded"


def test_premarket_refresh_failure_invalidates_prior_plan(paper_root: Path) -> None:
    from scripts.discovery.ai_instrument_allocator_pipeline import (
        AiInstrumentAllocatorPipeline,
    )

    class FailingNews:
        @staticmethod
        def search(*_args, **_kwargs):
            raise RuntimeError("Exa unavailable")

    config = load_runtime_config(paper_root)
    tracker = UsageTracker()
    pipeline = AiInstrumentAllocatorPipeline(
        paper_root,
        config,
        MockProvider(tracker),
        tracker,
        discovery_adapter=_AllocatorResearchDiscovery(),
        news_adapter=_AllocatorResearchNews(),
        option_data=_AllocatorNoOptions(),
    )
    overnight = pipeline.run_stage("overnight", "2026-07-13T00:00:00+00:00")
    old_plan_id = overnight["plans"][0]["plan_id"]
    pipeline.news = FailingNews()
    pipeline.discovery = _MustNotDiscover()

    updated = pipeline.run_stage("premarket_update", "2026-07-13T12:00:00+00:00")

    assert updated["model_calls"] == 0
    assert pipeline.plans.active_plans(OPEN_EXECUTION_NOW) == []
    assert pipeline.plans.plans()[old_plan_id]["status"] == "invalidated"


def test_premarket_state_is_safe_before_decision_audit_append(
    paper_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.discovery import ai_instrument_allocator_pipeline as pipeline_module

    config = load_runtime_config(paper_root)
    tracker = UsageTracker()
    news = _AllocatorResearchNews()
    pipeline = pipeline_module.AiInstrumentAllocatorPipeline(
        paper_root,
        config,
        MockProvider(tracker),
        tracker,
        discovery_adapter=_AllocatorResearchDiscovery(),
        news_adapter=news,
        option_data=_AllocatorNoOptions(),
    )
    overnight = pipeline.run_stage("overnight", "2026-07-13T00:00:00+00:00")
    old_plan_id = overnight["plans"][0]["plan_id"]
    news.direction = "negative"
    pipeline.discovery = _MustNotDiscover()
    monkeypatch.setattr(
        pipeline_module,
        "append_jsonl",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("disk full")),
    )

    with pytest.raises(RuntimeError, match="disk full"):
        pipeline.run_stage("premarket_update", "2026-07-13T12:00:00+00:00")

    active = pipeline.plans.active_plans(OPEN_EXECUTION_NOW)
    assert len(active) == 1
    assert active[0]["plan_id"] != old_plan_id
    assert pipeline.plans.plans()[old_plan_id]["status"] == "superseded"


def test_premarket_no_trade_invalidates_older_plan_for_same_ticker(
    paper_root: Path,
) -> None:
    from scripts.discovery.ai_instrument_allocator_pipeline import (
        AiInstrumentAllocatorPipeline,
    )

    config = load_runtime_config(paper_root)
    tracker = UsageTracker()
    news = _AllocatorResearchNews()
    pipeline = AiInstrumentAllocatorPipeline(
        paper_root,
        config,
        MockProvider(tracker),
        tracker,
        discovery_adapter=_AllocatorResearchDiscovery(),
        news_adapter=news,
        option_data=_AllocatorNoOptions(),
    )

    overnight = pipeline.run_stage("overnight", "2026-07-13T00:00:00+00:00")
    old_plan_id = overnight["plans"][0]["plan_id"]
    news.direction = "negative"
    news.already_priced_in = True
    pipeline.discovery = _MustNotDiscover()

    updated = pipeline.run_stage(
        "premarket_update",
        "2026-07-13T12:00:00+00:00",
    )

    assert updated["model_calls"] == 3
    assert updated["plans"] == []
    assert pipeline.plans.active_plans(OPEN_EXECUTION_NOW) == []
    assert pipeline.plans.plans()[old_plan_id]["status"] == "invalidated"


def test_preopen_revalidation_only_runs_news_and_challenge_for_new_evidence(
    paper_root: Path,
) -> None:
    from scripts.discovery.ai_instrument_allocator_pipeline import (
        AiInstrumentAllocatorPipeline,
    )

    class RecordingMockProvider(MockProvider):
        def __init__(self, tracker):
            super().__init__(tracker)
            self.requests = []

        def generate(self, request):
            self.requests.append(request)
            return super().generate(request)

    config = load_runtime_config(paper_root)
    tracker = UsageTracker()
    provider = RecordingMockProvider(tracker)
    news = _AllocatorResearchNews()
    pipeline = AiInstrumentAllocatorPipeline(
        paper_root,
        config,
        provider,
        tracker,
        discovery_adapter=_AllocatorResearchDiscovery(),
        news_adapter=news,
        option_data=_AllocatorNoOptions(),
    )

    overnight = pipeline.run_stage("overnight", "2026-07-13T00:00:00+00:00")
    old_plan_id = overnight["plans"][0]["plan_id"]
    news.direction = "negative"
    pipeline.discovery = _MustNotDiscover()
    calls_before = len(tracker.records)

    revalidated = pipeline.run_stage(
        "preopen_revalidation",
        "2026-07-13T13:25:00+00:00",
    )

    assert revalidated["model_calls"] == 2
    assert [
        record.agent_name for record in tracker.records[calls_before:]
    ] == [
        "ai_allocator_fast_news_agent",
        "ai_allocator_fast_challenge_agent",
    ]
    assert all(
        [event["headline"] for event in request.input_payload["available_news"]]
        == ["Company withdraws full-year guidance"]
        for request in provider.requests[calls_before:]
    )
    assert revalidated["paper_orders_created"] == 0
    assert revalidated["plans"] == []
    assert pipeline.plans.active_plans(OPEN_EXECUTION_NOW) == []
    assert pipeline.plans.plans()[old_plan_id]["status"] == "invalidated"


def test_preopen_invalidation_is_safe_before_decision_audit_append(
    paper_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.discovery import ai_instrument_allocator_pipeline as pipeline_module

    config = load_runtime_config(paper_root)
    tracker = UsageTracker()
    news = _AllocatorResearchNews()
    pipeline = pipeline_module.AiInstrumentAllocatorPipeline(
        paper_root,
        config,
        MockProvider(tracker),
        tracker,
        discovery_adapter=_AllocatorResearchDiscovery(),
        news_adapter=news,
        option_data=_AllocatorNoOptions(),
    )
    overnight = pipeline.run_stage("overnight", "2026-07-13T00:00:00+00:00")
    old_plan_id = overnight["plans"][0]["plan_id"]
    news.direction = "negative"
    pipeline.discovery = _MustNotDiscover()
    monkeypatch.setattr(
        pipeline_module,
        "append_jsonl",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("disk full")),
    )

    with pytest.raises(RuntimeError, match="disk full"):
        pipeline.run_stage(
            "preopen_revalidation",
            "2026-07-13T13:25:00+00:00",
        )

    assert pipeline.plans.active_plans(OPEN_EXECUTION_NOW) == []
    assert pipeline.plans.plans()[old_plan_id]["status"] == "invalidated"


def test_preopen_state_write_failure_blocks_open_execution(
    paper_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.discovery.ai_instrument_allocator_pipeline import (
        AiInstrumentAllocatorPipeline,
    )

    config = load_runtime_config(paper_root)
    tracker = UsageTracker()
    pipeline = AiInstrumentAllocatorPipeline(
        paper_root,
        config,
        MockProvider(tracker),
        tracker,
        discovery_adapter=_AllocatorResearchDiscovery(OPEN_EXECUTION_NOW),
        news_adapter=_AllocatorResearchNews(),
        option_data=_AllocatorNoOptions(),
    )
    pipeline.run_stage("overnight", "2026-07-13T00:00:00+00:00")
    monkeypatch.setattr(
        pipeline.plans.store,
        "write_json",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("disk full")),
    )

    with pytest.raises(RuntimeError, match="disk full"):
        pipeline.run_stage(
            "preopen_revalidation",
            "2026-07-13T13:25:00+00:00",
        )

    opened = pipeline.run_stage("open_execution", OPEN_EXECUTION_NOW)

    assert opened["paper_orders_created"] == 0
    assert opened["executions"][0]["reason"] == "current pre-open revalidation is required"
    assert pipeline.broker.store.orders() == {}


def test_no_trade_event_enters_cooldown_after_model_research(
    paper_root: Path,
) -> None:
    from scripts.discovery.ai_instrument_allocator_pipeline import (
        AiInstrumentAllocatorPipeline,
    )

    config = load_runtime_config(paper_root)
    tracker = UsageTracker()
    pipeline = AiInstrumentAllocatorPipeline(
        paper_root,
        config,
        MockProvider(tracker),
        tracker,
        discovery_adapter=_AllocatorResearchDiscovery(),
        news_adapter=_AllocatorResearchNews(already_priced_in=True),
        option_data=_AllocatorNoOptions(),
    )

    first = pipeline.run_stage("overnight", "2026-07-13T00:00:00+00:00")
    repeated = pipeline.run_stage(
        "premarket_update",
        "2026-07-13T12:00:00+00:00",
    )

    assert first["model_calls"] == 4
    assert first["plans"] == []
    assert repeated["model_calls"] == 0
    assert repeated["plans"] == []


def test_rank_only_event_enters_cooldown_after_successful_ranking(
    paper_root: Path,
) -> None:
    from scripts.discovery.ai_instrument_allocator_pipeline import (
        AiInstrumentAllocatorPipeline,
    )

    config = load_runtime_config(paper_root)
    config["strategies"]["ai_instrument_allocator_v1"][
        "top_deep_research_candidates"
    ] = 0
    tracker = UsageTracker()
    pipeline = AiInstrumentAllocatorPipeline(
        paper_root,
        config,
        MockProvider(tracker),
        tracker,
        discovery_adapter=_AllocatorResearchDiscovery(),
        news_adapter=_AllocatorResearchNews(),
        option_data=_AllocatorNoOptions(),
    )

    ranked = pipeline.run_stage("overnight", "2026-07-13T00:00:00+00:00")
    repeated = pipeline.run_stage(
        "premarket_update",
        "2026-07-13T12:00:00+00:00",
    )

    assert ranked["model_calls"] == 1
    assert ranked["plans"] == []
    assert repeated["model_calls"] == 0


def test_allocator_explicit_replay_rejects_future_option_quote(
    paper_root: Path,
) -> None:
    from scripts.discovery.ai_instrument_allocator_pipeline import (
        AiInstrumentAllocatorPipeline,
    )

    config = load_runtime_config(paper_root)
    config["strategies"]["ai_instrument_allocator_v1"][
        "option_minimum_scenario_return_pct"
    ] = -1
    tracker = UsageTracker()
    pipeline = AiInstrumentAllocatorPipeline(
        paper_root,
        config,
        MockProvider(tracker),
        tracker,
        discovery_adapter=_AllocatorExecutionDiscovery(OPEN_EXECUTION_NOW),
        news_adapter=_NoNews(),
        option_data=_AllocatorFutureOptionData(),
    )
    pipeline.plans.save_plan(
        {
            "plan_id": "future-option-quote-plan",
            "strategy": "ai_instrument_allocator_v1",
            "ticker": "AAPL",
            "created_at": "2026-07-13T13:27:00+00:00",
            "valid_until": "2026-07-13T13:37:00+00:00",
            "status": "active",
            "stage": "overnight",
            "preopen_revalidated_at": "2026-07-13T13:25:00+00:00",
            "signal": {
                **_bearish_signal(),
                "forecast_reference_price": 100.0,
                "forecast_reference_time": "2026-07-13T13:27:00+00:00",
            },
            "snapshot": {"snapshot_id": "future-option-quote-snapshot"},
        }
    )

    result = pipeline.run_stage("open_execution", OPEN_EXECUTION_NOW)
    execution = result["executions"][0]

    assert execution["status"] == "no_trade"
    assert "future" in execution["reason"]
    assert pipeline.broker.store.orders() == {}
    assert pipeline.option_broker.store.orders() == {}


def test_allocator_live_execution_accepts_quote_seen_after_stage_start(
    paper_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.discovery import ai_instrument_allocator_pipeline as allocator_module
    from scripts.discovery.ai_instrument_allocator_pipeline import (
        AiInstrumentAllocatorPipeline,
    )

    post_fetch = "2026-07-13T13:32:06+00:00"
    config = load_runtime_config(paper_root)
    config["strategies"]["ai_instrument_allocator_v1"][
        "option_minimum_scenario_return_pct"
    ] = -1
    tracker = UsageTracker()
    pipeline = AiInstrumentAllocatorPipeline(
        paper_root,
        config,
        MockProvider(tracker),
        tracker,
        discovery_adapter=_AllocatorExecutionDiscovery(OPEN_EXECUTION_NOW),
        news_adapter=_NoNews(),
        option_data=_AllocatorFutureOptionData(),
    )
    pipeline.plans.save_plan({
        "plan_id": "live-delayed-option-plan",
        "strategy": "ai_instrument_allocator_v1",
        "ticker": "AAPL",
        "created_at": "2026-07-13T13:27:00+00:00",
        "valid_until": "2026-07-13T13:37:00+00:00",
        "status": "active",
        "stage": "overnight",
        "preopen_revalidated_at": "2026-07-13T13:25:00+00:00",
        "signal": {
            **_bearish_signal(),
            "forecast_reference_price": 100.0,
            "forecast_reference_time": OPEN_EXECUTION_NOW,
        },
        "snapshot": {"snapshot_id": "live-delayed-option-snapshot"},
    })
    observed_monitor_times: list[str | None] = []
    monkeypatch.setattr(
        pipeline,
        "monitor_only",
        lambda now: observed_monitor_times.append(now)
        or {"event": "live-clock-monitor-stub"},
    )
    times = iter(
        [
            OPEN_EXECUTION_NOW,
            OPEN_EXECUTION_NOW,
            "2026-07-13T13:32:03+00:00",
            post_fetch,
        ]
    )
    monkeypatch.setattr(allocator_module, "utc_now", lambda: next(times))

    result = pipeline.run_stage("open_execution")
    execution = result["executions"][0]

    assert observed_monitor_times == [None]
    assert execution["status"] == "filled"
    assert execution["order"]["updated_at"] == post_fetch
    assert execution["allocation"]["decision_time"] == post_fetch
    assert execution["allocation"]["data_cutoff_time"] == post_fetch
    assert execution["allocation"]["account_nav_snapshot"]["calculated_at"] == (
        "2026-07-13T13:32:03+00:00"
    )
    assert execution["live_order_tools_called"] is False


@pytest.mark.parametrize(
    ("stage", "now"),
    [
        ("overnight", "2026-07-14T00:00:00+00:00"),
        ("premarket_update", "2026-07-13T12:00:00+00:00"),
        ("preopen_revalidation", "2026-07-13T13:25:00+00:00"),
    ],
)
def test_allocator_research_stages_never_create_orders(
    paper_root: Path,
    stage: str,
    now: str,
) -> None:
    from scripts.discovery.ai_instrument_allocator_pipeline import (
        AiInstrumentAllocatorPipeline,
    )

    config = load_runtime_config(paper_root)
    tracker = UsageTracker()
    pipeline = AiInstrumentAllocatorPipeline(
        paper_root,
        config,
        MockProvider(tracker),
        tracker,
        discovery_adapter=_AllocatorExecutionDiscovery(OPEN_EXECUTION_NOW),
        news_adapter=_NoNews(),
        option_data=_AllocatorNoOptions(),
    )

    result = pipeline.run_stage(stage, now)

    assert result["paper_orders_created"] == 0
    assert pipeline.broker.store.orders() == {}
    assert pipeline.option_broker.store.orders() == {}


def test_allocator_missing_mandate_exits_after_restart(paper_root: Path) -> None:
    from scripts.discovery.ai_instrument_allocator_pipeline import (
        AiInstrumentAllocatorPipeline,
    )

    config = load_runtime_config(paper_root)
    tracker = UsageTracker()
    pipeline = AiInstrumentAllocatorPipeline(
        paper_root,
        config,
        MockProvider(tracker),
        tracker,
        discovery_adapter=_AllocatorExecutionDiscovery(OPEN_EXECUTION_NOW),
        news_adapter=_NoNews(),
        option_data=_AllocatorNoOptions(),
    )
    pipeline.plans.save_plan(
        {
            "plan_id": "missing-mandate-plan",
            "strategy": "ai_instrument_allocator_v1",
            "ticker": "AAPL",
            "created_at": "2026-07-13T13:27:00+00:00",
            "valid_until": "2026-07-13T13:37:00+00:00",
            "status": "active",
            "stage": "overnight",
            "preopen_revalidated_at": "2026-07-13T13:25:00+00:00",
            "signal": _anchored_signal(
                forecast_reference_time="2026-07-13T13:27:00+00:00",
            ),
            "snapshot": {"snapshot_id": "missing-mandate-snapshot"},
        }
    )
    opened = pipeline.run_stage("open_execution", OPEN_EXECUTION_NOW)
    assert opened["paper_orders_created"] == 1, opened
    pipeline.mandates.store.write_json("position_mandates.json", {})

    restarted = AiInstrumentAllocatorPipeline(
        paper_root,
        config,
        MockProvider(tracker),
        tracker,
        discovery_adapter=_AllocatorExecutionDiscovery(OPEN_EXECUTION_NOW),
        news_adapter=_NoNews(),
        option_data=_AllocatorNoOptions(),
    )
    result = restarted.monitor_only(OPEN_EXECUTION_NOW)

    assert restarted.broker.store.positions() == {}
    assert result["exits"][0]["reason"] == "missing position mandate; fail closed"


def test_calibration_walk_forward_is_horizon_separated_and_maturity_safe() -> None:
    from scripts.evaluation.probability_calibration import (
        build_expanding_walk_forward_splits,
    )

    records = [
        {
            "record_id": "next-1",
            "horizon": "next_close",
            "decision_time": "2026-07-01T15:00:00+00:00",
            "label_matured_at": "2026-07-02T20:00:00+00:00",
            "actual_bucket": "return_plus_0_5_to_plus_2_pct",
        },
        {
            "record_id": "intraday",
            "horizon": "intraday_close",
            "decision_time": "2026-07-02T15:00:00+00:00",
            "label_matured_at": "2026-07-02T20:00:00+00:00",
            "actual_bucket": "return_minus_0_5_to_plus_0_5_pct",
        },
        {
            "record_id": "next-not-mature-at-cutoff",
            "horizon": "next_close",
            "decision_time": "2026-07-02T15:00:00+00:00",
            "label_matured_at": "2026-07-03T16:00:00+00:00",
            "actual_bucket": "return_plus_2_to_plus_5_pct",
        },
        {
            "record_id": "next-test",
            "horizon": "next_close",
            "decision_time": "2026-07-03T15:00:00+00:00",
            "label_matured_at": "2026-07-06T20:00:00+00:00",
            "actual_bucket": "return_minus_0_5_to_plus_0_5_pct",
        },
    ]

    folds = build_expanding_walk_forward_splits(
        records,
        horizon="next_close",
        evaluation_time="2026-07-07T00:00:00+00:00",
        minimum_train_size=1,
    )
    target = next(fold for fold in folds if fold["test_record"]["record_id"] == "next-test")

    assert target["horizon"] == "next_close"
    assert [item["record_id"] for item in target["training_records"]] == ["next-1"]
    assert all(item["horizon"] == "next_close" for item in target["training_records"])
    assert all(
        item["label_matured_at"] <= target["training_cutoff_time"]
        for item in target["training_records"]
    )


def test_multiclass_brier_and_log_loss_match_exact_fixture() -> None:
    import math

    from scripts.evaluation.probability_calibration import (
        multiclass_brier_score,
        multiclass_log_loss,
    )

    probabilities = {"down": 0.1, "flat": 0.2, "up": 0.7}

    assert multiclass_brier_score(probabilities, "up") == pytest.approx(0.14)
    assert multiclass_log_loss(probabilities, "up") == pytest.approx(-math.log(0.7))


def test_round_trip_cost_decomposition_matches_executable_net_pnl() -> None:
    from scripts.evaluation.calculate_metrics import round_trip_cost_decomposition

    equity = [
        {
            "fill": {
                "symbol": "AAPL",
                "side": "buy",
                "quantity": 2,
                "price": 100.11,
                "commission": 0.20,
                "filled_at": "2026-07-13T15:00:00+00:00",
            },
            "quote": {"bid": 99.90, "ask": 100.10},
        },
        {
            "fill": {
                "symbol": "AAPL",
                "side": "sell",
                "quantity": 2,
                "price": 101.89,
                "commission": 0.20,
                "filled_at": "2026-07-13T16:00:00+00:00",
            },
            "quote": {"bid": 101.90, "ask": 102.10},
        },
    ]
    options = [
        {
            "fill": {
                "option_id": "put-1",
                "intent": "buy_to_open",
                "quantity": 1,
                "multiplier": 100,
                "price": 1.03,
                "commission": 0,
                "filled_at": "2026-07-13T15:00:00+00:00",
            },
            "quote": {"bid": 0.98, "ask": 1.02},
        },
        {
            "fill": {
                "option_id": "put-1",
                "intent": "sell_to_close",
                "quantity": 1,
                "multiplier": 100,
                "price": 1.47,
                "commission": 0,
                "filled_at": "2026-07-13T16:00:00+00:00",
            },
            "quote": {"bid": 1.48, "ask": 1.52},
        },
    ]

    costs = round_trip_cost_decomposition(equity, options)

    assert costs["closed_round_trip_count"] == 2
    assert costs["gross_midpoint_pnl_usd"] == pytest.approx(54.0)
    assert costs["spread_cost_usd"] == pytest.approx(4.4)
    assert costs["slippage_and_tick_cost_usd"] == pytest.approx(2.04)
    assert costs["commission_usd"] == pytest.approx(0.4)
    assert costs["executable_net_pnl_usd"] == pytest.approx(47.16)
    assert costs["identity_residual_usd"] == pytest.approx(0.0, abs=1e-9)


def _open_allocator_mandate(
    exposure_id: str,
    *,
    ticker: str = "AAPL",
    instrument_type: str = "equity",
    horizon: str = "two_to_five_days",
    max_holding_trading_days: int = 5,
    opened_at: str = "2026-07-10T13:32:00+00:00",
    planned_exit_at: str = "2026-07-17T19:50:00+00:00",
    thesis_valid_until: str | None = None,
    planned_stop_price: float | None = None,
) -> dict:
    return {
        "mandate_version": 2,
        "exposure_id": exposure_id,
        "order_id": f"seed-{exposure_id}",
        "strategy": "ai_instrument_allocator_v1",
        "snapshot_id": f"seed-{ticker}",
        "ticker": ticker,
        "instrument_type": instrument_type,
        "horizon": horizon,
        "max_holding_trading_days": max_holding_trading_days,
        "status": "open",
        "created_at": opened_at,
        "entered_at": opened_at,
        "planned_exit_at": planned_exit_at,
        "thesis_valid_until": thesis_valid_until or planned_exit_at,
        "invalidation_condition": "Recorded research condition.",
        "invalidation_triggered": False,
        "planned_stop_price": (
            planned_stop_price
            if planned_stop_price is not None
            else 97.0
            if instrument_type == "equity"
            else None
        ),
        "closed_at": None,
        "close_reason": None,
    }


@pytest.mark.parametrize(
    (
        "horizon",
        "holding_days",
        "created_at",
        "planned_exit_at",
        "thesis_valid_until",
        "valid",
    ),
    [
        (
            "intraday_close",
            0,
            "2026-07-13T15:00:00+00:00",
            "2026-07-13T19:50:00+00:00",
            "2026-07-13T19:50:00+00:00",
            True,
        ),
        (
            "next_close",
            1,
            "2026-07-02T15:00:00+00:00",
            "2026-07-06T19:50:00+00:00",
            "2026-07-06T19:50:00+00:00",
            True,
        ),
        (
            "two_to_five_days",
            5,
            "2026-07-10T15:00:00+00:00",
            "2026-07-17T19:50:00+00:00",
            "2026-07-17T19:50:00+00:00",
            True,
        ),
        (
            "intraday_close",
            0,
            "2026-07-13T15:00:00+00:00",
            "2026-07-14T19:50:00+00:00",
            "2026-07-14T19:50:00+00:00",
            False,
        ),
        (
            "next_close",
            1,
            "2026-07-02T15:00:00+00:00",
            "2026-07-07T19:50:00+00:00",
            "2026-07-07T19:50:00+00:00",
            False,
        ),
        (
            "two_to_five_days",
            5,
            "2026-07-10T15:00:00+00:00",
            "2026-07-16T19:50:00+00:00",
            "2026-07-16T19:50:00+00:00",
            False,
        ),
        (
            "two_to_five_days",
            5,
            "2026-07-10T15:00:00+00:00",
            "2026-07-20T19:50:00+00:00",
            "2026-07-20T19:50:00+00:00",
            False,
        ),
        (
            "next_close",
            1,
            "2026-07-13T15:00:00+00:00",
            "2026-07-14T19:50:00+00:00",
            "2026-07-14T19:49:59+00:00",
            False,
        ),
        (
            "next_close",
            1,
            "2026-07-13T15:00:00+00:00",
            "2026-07-14T12:00:00+00:00",
            "2026-07-14T20:00:00+00:00",
            False,
        ),
    ],
)
def test_allocator_persisted_mandate_semantics_are_exchange_session_consistent(
    horizon: str,
    holding_days: int,
    created_at: str,
    planned_exit_at: str,
    thesis_valid_until: str,
    valid: bool,
) -> None:
    from scripts.exit.position_mandates import evaluate_mandate_exit

    mandate = _open_allocator_mandate(
        "equity:AAPL",
        horizon=horizon,
        max_holding_trading_days=holding_days,
        opened_at=created_at,
        planned_exit_at=planned_exit_at,
        thesis_valid_until=thesis_valid_until,
    )

    decision = evaluate_mandate_exit(mandate, created_at)

    assert decision.should_exit is (not valid)
    assert decision.reason == (
        "position mandate remains valid"
        if valid
        else "invalid position mandate; fail closed"
    )


def test_allocator_legacy_v1_mandate_accepts_valid_two_to_five_session_range() -> None:
    from scripts.exit.position_mandates import evaluate_mandate_exit

    mandate = _open_allocator_mandate("equity:AAPL")
    mandate["mandate_version"] = 1
    mandate.pop("max_holding_trading_days")

    decision = evaluate_mandate_exit(
        mandate,
        "2026-07-15T15:00:00+00:00",
    )

    assert decision.should_exit is False
    assert decision.reason == "position mandate remains valid"


def test_allocator_register_rejects_semantically_inconsistent_mandate(
    paper_root: Path,
) -> None:
    from scripts.exit.position_mandates import PositionMandateStore

    store = PositionMandateStore(paper_root)

    with pytest.raises(ValueError, match="position mandate semantics are invalid"):
        store.register_order(
            order_id="bad-intraday",
            exposure_id="equity:AAPL",
            strategy="ai_instrument_allocator_v1",
            snapshot_id="snapshot-bad-intraday",
            ticker="AAPL",
            instrument_type="equity",
            horizon="intraday_close",
            max_holding_trading_days=0,
            created_at="2026-07-13T15:00:00+00:00",
            planned_exit_at="2026-07-14T19:50:00+00:00",
            thesis_valid_until="2026-07-14T19:50:00+00:00",
            invalidation_condition="Recorded research condition.",
            planned_stop_price=97.0,
        )

    assert store.mandates() == {}


class _AllocatorMonitoringOptions(_AllocatorNoOptions):
    def __init__(self, quote_asof: str) -> None:
        self.quote_asof = quote_asof

    def fetch_quotes(self, option_ids):
        return {
            option_id: replace(
                _option_quote(option_id, bid=1.0, ask=1.01),
                updated_at=self.quote_asof,
            )
            for option_id in option_ids
        }


def test_allocator_equity_monitor_uses_mandate_trading_horizon_not_calendar_stop(
    paper_root: Path,
) -> None:
    from scripts.core.models import Position
    from scripts.discovery.ai_instrument_allocator_pipeline import (
        AiInstrumentAllocatorPipeline,
    )

    opened_at = "2026-07-10T13:32:00+00:00"
    wednesday = "2026-07-15T15:00:00+00:00"
    planned_exit_at = "2026-07-17T19:50:00+00:00"
    config = load_runtime_config(paper_root)
    tracker = UsageTracker()
    discovery = _AllocatorExecutionDiscovery(wednesday)
    pipeline = AiInstrumentAllocatorPipeline(
        paper_root,
        config,
        MockProvider(tracker),
        tracker,
        discovery_adapter=discovery,
        news_adapter=_NoNews(),
        option_data=_AllocatorNoOptions(),
    )
    pipeline.broker.store.save_positions(
        {"AAPL": Position("AAPL", 1.0, 100.0, opened_at, opened_at)}
    )
    pipeline.mandates.store.write_json(
        "position_mandates.json",
        {
            "equity:AAPL": _open_allocator_mandate(
                "equity:AAPL",
                planned_exit_at=planned_exit_at,
            )
        },
    )

    held = pipeline.monitor_only(wednesday)

    assert held["exits"] == []
    assert set(pipeline.broker.store.positions()) == {"AAPL"}

    discovery.quote_asof = planned_exit_at
    exited = pipeline.monitor_only(planned_exit_at)

    assert pipeline.broker.store.positions() == {}
    assert exited["exits"][0]["reason"] == "position mandate planned exit reached"


def test_allocator_equity_monitor_uses_persisted_stop_after_config_change(
    paper_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.core.models import Position, Quote
    from scripts.discovery.ai_instrument_allocator_pipeline import (
        AiInstrumentAllocatorPipeline,
    )

    opened_at = "2026-07-13T15:00:00+00:00"
    config = load_runtime_config(paper_root)
    config["risk"]["stop_loss_pct"] = 0.01
    tracker = UsageTracker()
    discovery = _AllocatorExecutionDiscovery(opened_at)
    quote_state = {"bid": 96.0, "asof": opened_at}

    def fetch_quote(symbol: str, **_kwargs) -> Quote:
        bid = quote_state["bid"]
        return Quote(
            symbol,
            bid,
            bid + 0.01,
            bid + 0.005,
            quote_state["asof"],
            source="persisted-stop-test",
            avg_daily_volume_usd=500_000_000,
        )

    monkeypatch.setattr(discovery, "fetch_current_quote", fetch_quote)
    pipeline = AiInstrumentAllocatorPipeline(
        paper_root,
        config,
        MockProvider(tracker),
        tracker,
        discovery_adapter=discovery,
        news_adapter=_NoNews(),
        option_data=_AllocatorNoOptions(),
    )
    pipeline.broker.store.save_positions(
        {"AAPL": Position("AAPL", 1.0, 100.0, opened_at, opened_at)}
    )
    pipeline.mandates.store.write_json(
        "position_mandates.json",
        {
            "equity:AAPL": _open_allocator_mandate(
                "equity:AAPL",
                horizon="next_close",
                max_holding_trading_days=1,
                opened_at=opened_at,
                planned_exit_at="2026-07-14T19:50:00+00:00",
                planned_stop_price=95.0,
            )
        },
    )

    held = pipeline.monitor_only(opened_at)

    assert held["exits"] == []
    assert set(pipeline.broker.store.positions()) == {"AAPL"}

    stop_time = "2026-07-13T15:01:00+00:00"
    quote_state.update({"bid": 95.0, "asof": stop_time})
    exited = pipeline.monitor_only(stop_time)

    assert pipeline.broker.store.positions() == {}
    assert exited["exits"][0]["reason"] == "deterministic stop loss"


def test_allocator_option_monitor_uses_mandate_trading_horizon_not_calendar_stop(
    paper_root: Path,
) -> None:
    from scripts.discovery.ai_instrument_allocator_pipeline import (
        AiInstrumentAllocatorPipeline,
    )
    from scripts.options.models import OptionPosition

    opened_at = "2026-07-10T13:32:00+00:00"
    wednesday = "2026-07-15T15:00:00+00:00"
    planned_exit_at = "2026-07-17T19:50:00+00:00"
    contract = _option_contract("aapl-five-session-call", "2026-08-21")
    config = load_runtime_config(paper_root)
    tracker = UsageTracker()
    option_data = _AllocatorMonitoringOptions(wednesday)
    pipeline = AiInstrumentAllocatorPipeline(
        paper_root,
        config,
        MockProvider(tracker),
        tracker,
        discovery_adapter=_AllocatorExecutionDiscovery(wednesday),
        news_adapter=_NoNews(),
        option_data=option_data,
    )
    pipeline.option_broker.store.save_positions(
        {
            contract.option_id: OptionPosition(
                contract,
                1,
                1.0,
                opened_at,
                opened_at,
            )
        }
    )
    pipeline.mandates.store.write_json(
        "position_mandates.json",
        {
            f"option:{contract.option_id}": _open_allocator_mandate(
                f"option:{contract.option_id}",
                instrument_type="call",
                planned_exit_at=planned_exit_at,
            )
        },
    )

    held = pipeline.monitor_only(wednesday)

    assert held["option_exits"] == []
    assert set(pipeline.option_broker.store.positions()) == {contract.option_id}

    option_data.quote_asof = planned_exit_at
    exited = pipeline.monitor_only(planned_exit_at)

    assert pipeline.option_broker.store.positions() == {}
    assert (
        exited["option_exits"][0]["reason"]
        == "position mandate planned exit reached"
    )


def test_legacy_exit_evaluators_keep_calendar_time_stop_enabled_by_default() -> None:
    from scripts.core.models import Position, Quote
    from scripts.exit.evaluate_exit import evaluate_position_exit
    from scripts.options.exit_policy import evaluate_option_exit
    from scripts.options.models import OptionPosition

    opened_at = "2026-07-10T13:32:00+00:00"
    wednesday = "2026-07-15T15:00:00+00:00"
    equity = Position("AAPL", 1.0, 100.0, opened_at, opened_at)
    equity_quote = Quote("AAPL", 100.0, 100.01, 100.005, wednesday, "fixture")
    option = OptionPosition(
        _option_contract("legacy-calendar-call", "2026-08-21"),
        1,
        1.0,
        opened_at,
        opened_at,
    )
    option_quote = replace(
        _option_quote(option.contract.option_id, bid=1.0, ask=1.01),
        updated_at=wednesday,
    )

    equity_exit = evaluate_position_exit(
        equity,
        equity_quote,
        wednesday,
        {"max_holding_calendar_days": 5},
    )
    option_exit = evaluate_option_exit(
        option,
        option_quote,
        wednesday,
        {"max_holding_calendar_days": 5, "force_exit_dte": 2},
    )

    assert equity_exit.reason == "deterministic time stop"
    assert option_exit.reason == "maximum option holding period reached"

    equity_stop = evaluate_position_exit(
        equity,
        replace(equity_quote, bid=96.0, ask=96.01, last=96.005),
        wednesday,
        {"stop_loss_pct": 0.03, "max_holding_calendar_days": 5},
        apply_legacy_time_stop=False,
    )
    option_stop = evaluate_option_exit(
        option,
        replace(option_quote, bid=0.5, ask=0.51),
        wednesday,
        {
            "stop_loss_pct_of_premium": 0.35,
            "max_holding_calendar_days": 5,
            "force_exit_dte": 2,
        },
        apply_legacy_time_stop=False,
    )

    assert equity_stop.reason == "deterministic stop loss"
    assert option_stop.reason == "option premium stop loss reached"


@pytest.mark.parametrize(
    ("horizon", "holding_days", "decision_time", "planned_exit_at"),
    [
        (
            "intraday_close",
            0,
            "2026-07-13T15:00:00+00:00",
            "2026-07-13T19:50:00+00:00",
        ),
        (
            "next_close",
            1,
            "2026-07-02T15:00:00+00:00",
            "2026-07-06T19:50:00+00:00",
        ),
        (
            "two_to_five_days",
            5,
            "2026-07-10T15:00:00+00:00",
            "2026-07-17T19:50:00+00:00",
        ),
    ],
)
@pytest.mark.parametrize(
    ("validity_offset_seconds", "allowed"),
    [(-1, False), (0, True), (1, True)],
)
def test_allocator_thesis_validity_must_cover_declared_horizon(
    paper_root: Path,
    horizon: str,
    holding_days: int,
    decision_time: str,
    planned_exit_at: str,
    validity_offset_seconds: int,
    allowed: bool,
) -> None:
    from datetime import timedelta

    from scripts.core.models import Quote, parse_ts
    from scripts.decision.instrument_allocator import allocate_instrument

    signal = _anchored_signal(
        horizon=horizon,
        max_holding_trading_days=holding_days,
        forecast_reference_time=decision_time,
        thesis_valid_until=(
            parse_ts(planned_exit_at) + timedelta(seconds=validity_offset_seconds)
        ).isoformat(),
    )
    quote = Quote(
        "AAPL",
        100.0,
        100.01,
        100.005,
        decision_time,
        source="horizon-test",
        avg_daily_volume_usd=500_000_000,
    )

    allocation = allocate_instrument(
        signal,
        quote,
        [],
        _account_state(),
        load_runtime_config(paper_root),
        decision_time,
        planned_exit_at=planned_exit_at,
    )

    if allowed:
        assert allocation["status"] == "selected"
    else:
        assert allocation["status"] == "no_trade"
        assert allocation["reason"] == (
            "invalid actionable signal: thesis validity does not cover "
            "declared signal horizon"
        )


@pytest.mark.parametrize(
    ("horizon", "holding_days", "decision_time", "thesis_valid_until"),
    [
        (
            "intraday_close",
            0,
            "2026-07-13T15:00:00+00:00",
            "2026-07-13T18:00:00+00:00",
        ),
        (
            "next_close",
            1,
            "2026-07-02T15:00:00+00:00",
            "2026-07-02T18:00:00+00:00",
        ),
        (
            "two_to_five_days",
            5,
            "2026-07-10T15:00:00+00:00",
            "2026-07-13T18:00:00+00:00",
        ),
    ],
)
def test_allocator_execution_fails_closed_before_order_when_thesis_expires_early(
    paper_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    horizon: str,
    holding_days: int,
    decision_time: str,
    thesis_valid_until: str,
) -> None:
    from scripts.discovery.ai_instrument_allocator_pipeline import (
        AiInstrumentAllocatorPipeline,
    )

    config = load_runtime_config(paper_root)
    tracker = UsageTracker()
    pipeline = AiInstrumentAllocatorPipeline(
        paper_root,
        config,
        MockProvider(tracker),
        tracker,
        discovery_adapter=_AllocatorExecutionDiscovery(decision_time),
        news_adapter=_NoNews(),
        option_data=_AllocatorNoOptions(),
    )
    monkeypatch.setattr(
        pipeline.broker,
        "create_order",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("invalid horizon must fail before paper order creation")
        ),
    )
    monkeypatch.setattr(
        pipeline.option_broker,
        "create_order",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("invalid horizon must fail before paper option order creation")
        ),
    )
    plan = {
        "plan_id": "early-thesis-expiry",
        "strategy": "ai_instrument_allocator_v1",
        "ticker": "AAPL",
        "stage": "intraday",
        "signal": _anchored_signal(
            horizon=horizon,
            max_holding_trading_days=holding_days,
            forecast_reference_time=decision_time,
            thesis_valid_until=thesis_valid_until,
        ),
        "snapshot": {"snapshot_id": "early-thesis-expiry"},
    }

    result = pipeline._execute_plan(plan, decision_time, stage="intraday")

    assert result["status"] == "no_trade"
    assert result["reason"] == (
        "invalid actionable signal: thesis validity does not cover declared "
        "signal horizon"
    )
    assert pipeline.broker.store.orders() == {}
    assert pipeline.option_broker.store.orders() == {}


def test_position_mandate_store_ignores_corrupt_root_and_individual_records(
    paper_root: Path,
) -> None:
    from scripts.exit.position_mandates import PositionMandateStore

    store = PositionMandateStore(paper_root)
    store.store.write_json("position_mandates.json", None)
    assert store.mandates() == {}

    valid = _open_allocator_mandate("equity:MSFT", ticker="MSFT")
    store.store.write_json(
        "position_mandates.json",
        {
            "equity:AAPL": None,
            "equity:NVDA": 7,
            "equity:TSLA": [],
            "equity:MSFT": valid,
        },
    )

    assert store.mandates() == {"equity:MSFT": valid}


@pytest.mark.parametrize("field", ["planned_exit_at", "thesis_valid_until"])
def test_allocator_malformed_mandate_timestamp_fails_closed(
    field: str,
) -> None:
    from scripts.exit.position_mandates import evaluate_mandate_exit

    mandate = _open_allocator_mandate("equity:AAPL")
    mandate[field] = "not-a-timestamp"

    decision = evaluate_mandate_exit(
        mandate,
        "2026-07-13T15:00:00+00:00",
    )

    assert decision.should_exit is True
    assert decision.reason == "invalid position mandate; fail closed"


def test_allocator_corrupt_mandate_record_exits_fail_closed_without_crashing(
    paper_root: Path,
) -> None:
    from scripts.core.models import Position
    from scripts.discovery.ai_instrument_allocator_pipeline import (
        AiInstrumentAllocatorPipeline,
    )

    decision_time = "2026-07-13T15:00:00+00:00"
    config = load_runtime_config(paper_root)
    tracker = UsageTracker()
    pipeline = AiInstrumentAllocatorPipeline(
        paper_root,
        config,
        MockProvider(tracker),
        tracker,
        discovery_adapter=_AllocatorExecutionDiscovery(decision_time),
        news_adapter=_NoNews(),
        option_data=_AllocatorNoOptions(),
    )
    pipeline.broker.store.save_positions(
        {
            "AAPL": Position(
                "AAPL",
                1.0,
                100.0,
                decision_time,
                decision_time,
            )
        }
    )
    pipeline.mandates.store.write_json(
        "position_mandates.json",
        {"equity:AAPL": None},
    )

    result = pipeline.monitor_only(decision_time)

    assert pipeline.broker.store.positions() == {}
    assert result["exits"][0]["reason"] == "missing position mandate; fail closed"


def _restart_test_allocator(
    paper_root: Path,
    *,
    now: str = "2026-07-13T13:30:00+00:00",
    discovery=None,
    option_data=None,
):
    from scripts.discovery.ai_instrument_allocator_pipeline import (
        AiInstrumentAllocatorPipeline,
    )

    config = load_runtime_config(paper_root)
    tracker = UsageTracker()
    return AiInstrumentAllocatorPipeline(
        paper_root,
        config,
        MockProvider(tracker),
        tracker,
        discovery_adapter=discovery or _AllocatorExecutionDiscovery(now),
        news_adapter=_NoNews(),
        option_data=option_data or _AllocatorNoOptions(),
    )


def _register_pending_equity_mandate(pipeline, order_id: str) -> None:
    pipeline.mandates.register_order(
        order_id=order_id,
        exposure_id="equity:AAPL",
        strategy="ai_instrument_allocator_v1",
        snapshot_id="restart-snapshot",
        ticker="AAPL",
        instrument_type="equity",
        horizon="next_close",
        max_holding_trading_days=1,
        created_at="2026-07-13T13:27:00+00:00",
        planned_exit_at="2026-07-14T19:50:00+00:00",
        thesis_valid_until="2026-07-14T19:50:00+00:00",
        invalidation_condition="Recorded research condition.",
        planned_stop_price=97.0,
    )


def _seed_active_restart_plan(
    pipeline,
    *,
    plan_id: str,
    allocation_id: str,
) -> None:
    pipeline.plans.save_plan(
        {
            "plan_id": plan_id,
            "strategy": "ai_instrument_allocator_v1",
            "ticker": "AAPL",
            "created_at": "2026-07-13T13:27:00+00:00",
            "valid_until": "2026-07-13T13:37:00+00:00",
            "status": "active",
            "stage": "overnight",
            "preopen_revalidated_at": "2026-07-13T13:25:00+00:00",
            "signal": _anchored_signal(
                entry_now=False,
                forecast_reference_time="2026-07-13T13:27:00+00:00",
            ),
            "snapshot": {"snapshot_id": "restart-snapshot"},
        }
    )
    pipeline.plans.record_allocation(
        {
            "allocation_id": allocation_id,
            "plan_id": plan_id,
        }
    )


@pytest.mark.parametrize("with_mandate", [False, True])
def test_allocator_restart_cancels_created_entry_in_both_crash_windows(
    paper_root: Path,
    with_mandate: bool,
) -> None:
    pipeline = _restart_test_allocator(paper_root)
    pipeline.plans.save_plan(
        {
            "plan_id": "crash-window-plan",
            "strategy": "ai_instrument_allocator_v1",
            "ticker": "AAPL",
            "created_at": "2026-07-13T13:27:00+00:00",
            "valid_until": "2026-07-14T19:50:00+00:00",
            "status": "active",
            "stage": "overnight",
            "signal": _anchored_signal(),
            "snapshot": {"snapshot_id": "restart-snapshot"},
        }
    )
    pipeline.plans.record_allocation(
        {
            "allocation_id": "crash-window-allocation",
            "plan_id": "crash-window-plan",
        }
    )
    order = pipeline.broker.create_order(
        decision_id="crash-window-allocation",
        symbol="AAPL",
        side="buy",
        order_type="limit",
        quantity=1,
        limit_price=100.01,
        quote_seen_at="2026-07-13T13:27:00+00:00",
        strategy="ai_instrument_allocator_v1",
        planned_stop_price=97.0,
        signal_horizon="next_close",
        now="2026-07-13T13:27:00+00:00",
    )
    if with_mandate:
        _register_pending_equity_mandate(pipeline, order.order_id)

    restarted = _restart_test_allocator(paper_root)
    restarted.monitor_only("2026-07-13T13:30:00+00:00")

    recovered = restarted.broker.store.orders()[order.order_id]
    assert recovered.status == "cancelled"
    assert "restart recovery" in str(recovered.reject_reason)
    mandate = restarted.mandates.for_exposure("equity:AAPL")
    assert mandate is None if not with_mandate else mandate["status"] == "closed"
    assert restarted.plans.active_plans("2026-07-13T13:30:00+00:00") == []


@pytest.mark.parametrize(
    ("status", "mandate_case", "expected_status"),
    [
        ("submitted_to_paper_broker", "missing", "cancelled"),
        ("open", "corrupt", "cancelled"),
        ("partially_filled", "missing", "cancelled"),
        ("open", "valid", "open"),
    ],
)
def test_allocator_restart_cancels_retryable_entry_without_valid_mandate(
    paper_root: Path,
    status: str,
    mandate_case: str,
    expected_status: str,
) -> None:
    pipeline = _restart_test_allocator(paper_root)
    order = pipeline.broker.create_order(
        decision_id=f"retry-{status}",
        symbol="AAPL",
        side="buy",
        order_type="limit",
        quantity=1,
        limit_price=99.0,
        quote_seen_at="2026-07-13T13:27:00+00:00",
        strategy="ai_instrument_allocator_v1",
        planned_stop_price=97.0,
        signal_horizon="next_close",
        now="2026-07-13T13:27:00+00:00",
    )
    order.status = status
    order.submitted_at = "2026-07-13T13:27:00+00:00"
    pipeline.broker.store.save_orders({order.order_id: order})
    if mandate_case in {"corrupt", "valid"}:
        _register_pending_equity_mandate(pipeline, order.order_id)
    if mandate_case == "corrupt":
        mandates = pipeline.mandates.mandates()
        mandates["equity:AAPL"]["instrument_type"] = "call"
        mandates["equity:AAPL"]["planned_stop_price"] = None
        pipeline.mandates.store.write_json("position_mandates.json", mandates)

    restarted = _restart_test_allocator(paper_root)
    restarted.monitor_only("2026-07-13T13:30:00+00:00")

    recovered = restarted.broker.store.orders()[order.order_id]
    assert recovered.status == expected_status
    if expected_status == "cancelled":
        assert recovered.reject_reason == "allocator entry mandate invalid during restart recovery"


def test_allocator_restart_cancels_orphan_created_option_entry(
    paper_root: Path,
) -> None:
    pipeline = _restart_test_allocator(paper_root)
    contract = _option_contract("orphan-option", "2026-08-21", "put")
    order = pipeline.option_broker.create_order(
        decision_id="orphan-option-allocation",
        contract=contract,
        intent="buy_to_open",
        order_type="limit",
        quantity=1,
        limit_price=1.0,
        quote_seen_at="2026-07-13T13:27:00+00:00",
        strategy="ai_instrument_allocator_v1",
        signal_horizon="next_close",
        now="2026-07-13T13:27:00+00:00",
    )

    restarted = _restart_test_allocator(paper_root)
    restarted.monitor_only("2026-07-13T13:30:00+00:00")

    assert restarted.option_broker.store.orders()[order.order_id].status == "cancelled"


@pytest.mark.parametrize(
    ("order_status", "mandate_status"),
    [
        pytest.param(
            "submitted_to_paper_broker",
            "pending_fill",
            id="after-submit-persistence",
        ),
        pytest.param("filled", "pending_fill", id="after-fill-persistence"),
        pytest.param(
            "filled",
            "open",
            id="after-mandate-reconcile-before-plan-status",
        ),
    ],
)
def test_allocator_restart_reconciles_persisted_entry_before_reexecution(
    paper_root: Path,
    order_status: str,
    mandate_status: str,
) -> None:
    from scripts.core.models import Account, Position

    pipeline = _restart_test_allocator(paper_root, now=OPEN_EXECUTION_NOW)
    plan_id = f"post-submit-{order_status}-{mandate_status}"
    allocation_id = f"allocation-{order_status}-{mandate_status}"
    _seed_active_restart_plan(
        pipeline,
        plan_id=plan_id,
        allocation_id=allocation_id,
    )
    order = pipeline.broker.create_order(
        decision_id=allocation_id,
        symbol="AAPL",
        side="buy",
        order_type="limit",
        quantity=1,
        limit_price=99.0,
        quote_seen_at="2026-07-13T13:31:00+00:00",
        strategy="ai_instrument_allocator_v1",
        planned_stop_price=97.0,
        signal_horizon="next_close",
        now="2026-07-13T13:31:00+00:00",
    )
    order.status = order_status
    order.submitted_at = "2026-07-13T13:31:00+00:00"
    order.updated_at = "2026-07-13T13:31:00+00:00"
    if order_status == "filled":
        order.filled_quantity = 1
        order.average_fill_price = 100.0
        pipeline.broker.store.save_account(
            Account(9_900, 10_000),
            "2026-07-13T13:31:00+00:00",
        )
        pipeline.broker.store.save_positions(
            {
                "AAPL": Position(
                    "AAPL",
                    1,
                    100.0,
                    "2026-07-13T13:31:00+00:00",
                    "2026-07-13T13:31:00+00:00",
                )
            }
        )
    pipeline.broker.store.save_orders({order.order_id: order})
    _register_pending_equity_mandate(pipeline, order.order_id)
    if mandate_status == "open":
        mandates = pipeline.mandates.mandates()
        mandates["equity:AAPL"]["status"] = "open"
        mandates["equity:AAPL"]["entered_at"] = order.updated_at
        pipeline.mandates.store.write_json("position_mandates.json", mandates)

    restarted = _restart_test_allocator(paper_root, now=OPEN_EXECUTION_NOW)
    result = restarted.run_stage("open_execution", OPEN_EXECUTION_NOW)

    assert result["paper_orders_created"] == 0
    assert len(restarted.broker.store.orders()) == 1
    assert restarted.plans.plans()[plan_id]["status"] == "executed"
    assert restarted.plans.active_plans(OPEN_EXECUTION_NOW) == []
    mandate = restarted.mandates.for_exposure("equity:AAPL")
    assert mandate["order_id"] == order.order_id
    assert mandate["status"] == ("open" if order_status == "filled" else "pending_fill")


@pytest.mark.parametrize("order_status", ["rejected", "expired", "cancelled"])
def test_allocator_restart_maps_terminal_entry_status_to_plan(
    paper_root: Path,
    order_status: str,
) -> None:
    pipeline = _restart_test_allocator(paper_root, now=OPEN_EXECUTION_NOW)
    plan_id = f"terminal-{order_status}"
    allocation_id = f"terminal-allocation-{order_status}"
    _seed_active_restart_plan(
        pipeline,
        plan_id=plan_id,
        allocation_id=allocation_id,
    )
    order = pipeline.broker.create_order(
        decision_id=allocation_id,
        symbol="AAPL",
        side="buy",
        order_type="limit",
        quantity=1,
        limit_price=99.0,
        quote_seen_at="2026-07-13T13:31:00+00:00",
        strategy="ai_instrument_allocator_v1",
        planned_stop_price=97.0,
        signal_horizon="next_close",
        now="2026-07-13T13:31:00+00:00",
    )
    order.status = order_status
    order.updated_at = "2026-07-13T13:31:00+00:00"
    pipeline.broker.store.save_orders({order.order_id: order})

    restarted = _restart_test_allocator(paper_root, now=OPEN_EXECUTION_NOW)
    restarted.monitor_only(OPEN_EXECUTION_NOW)

    assert restarted.plans.plans()[plan_id]["status"] == order_status
    assert restarted.plans.active_plans(OPEN_EXECUTION_NOW) == []


@pytest.mark.parametrize("existing_status", ["pending_fill", "open"])
def test_position_mandate_store_refuses_active_exposure_replacement(
    paper_root: Path,
    existing_status: str,
) -> None:
    pipeline = _restart_test_allocator(paper_root)
    _register_pending_equity_mandate(pipeline, "original-order")
    if existing_status == "open":
        mandates = pipeline.mandates.mandates()
        mandates["equity:AAPL"]["status"] = "open"
        mandates["equity:AAPL"]["entered_at"] = "2026-07-13T13:28:00+00:00"
        pipeline.mandates.store.write_json("position_mandates.json", mandates)

    _register_pending_equity_mandate(pipeline, "original-order")
    with pytest.raises(ValueError, match="active position mandate already exists"):
        _register_pending_equity_mandate(pipeline, "replacement-order")

    mandate = pipeline.mandates.for_exposure("equity:AAPL")
    assert mandate["order_id"] == "original-order"
    assert mandate["status"] == existing_status


@pytest.mark.parametrize(
    "changes",
    [
        {"exposure_id": "equity:MSFT", "ticker": "MSFT"},
        {
            "exposure_id": "option:wrong-instrument",
            "instrument_type": "call",
            "planned_stop_price": None,
        },
    ],
)
def test_allocator_equity_mandate_identity_mismatch_exits_without_exception(
    paper_root: Path,
    changes: dict,
) -> None:
    from scripts.core.models import Position

    pipeline = _restart_test_allocator(
        paper_root,
        now=REGULAR_NOW,
        discovery=_AllocatorExecutionDiscovery(REGULAR_NOW),
    )
    pipeline.broker.store.save_positions(
        {"AAPL": Position("AAPL", 1, 100, REGULAR_NOW, REGULAR_NOW)}
    )
    mandate = _open_allocator_mandate(
        "equity:AAPL",
        ticker="AAPL",
        instrument_type="equity",
        horizon="next_close",
        max_holding_trading_days=1,
        opened_at=REGULAR_NOW,
        planned_exit_at=NEXT_CLOSE_EXIT,
        planned_stop_price=97.0,
    )
    mandate.update(changes)
    pipeline.mandates.store.write_json(
        "position_mandates.json",
        {"equity:AAPL": mandate},
    )

    result = pipeline.monitor_only(REGULAR_NOW)

    assert pipeline.broker.store.positions() == {}
    assert result["exits"][0]["reason"] == "invalid position mandate; fail closed"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("exposure_id", "option:different-option"),
        ("ticker", "MSFT"),
        ("instrument_type", "put"),
    ],
)
def test_allocator_option_mandate_identity_mismatch_exits_fail_closed(
    paper_root: Path,
    field: str,
    value: str,
) -> None:
    from scripts.options.models import OptionPosition

    contract = _option_contract("identity-call", "2026-08-21", "call")
    pipeline = _restart_test_allocator(
        paper_root,
        now=REGULAR_NOW,
        option_data=_AllocatorMonitoringOptions(REGULAR_NOW),
    )
    pipeline.option_broker.store.save_positions(
        {
            contract.option_id: OptionPosition(
                contract,
                1,
                1.0,
                REGULAR_NOW,
                REGULAR_NOW,
            )
        }
    )
    mandate = _open_allocator_mandate(
        f"option:{contract.option_id}",
        ticker="AAPL",
        instrument_type="call",
        horizon="next_close",
        max_holding_trading_days=1,
        opened_at=REGULAR_NOW,
        planned_exit_at=NEXT_CLOSE_EXIT,
    )
    mandate[field] = value
    pipeline.mandates.store.write_json(
        "position_mandates.json",
        {f"option:{contract.option_id}": mandate},
    )

    result = pipeline.monitor_only(REGULAR_NOW)

    assert pipeline.option_broker.store.positions() == {}
    assert result["option_exits"][0]["reason"] == "invalid position mandate; fail closed"


def test_allocator_account_state_uses_fresh_liquidation_marks(
    paper_root: Path,
) -> None:
    from scripts.core.models import Account, Position, Quote
    from scripts.options.models import OptionPosition

    class MarkedDiscovery(_AllocatorExecutionDiscovery):
        def fetch_current_quote(self, symbol: str, **_kwargs):
            return Quote(
                symbol,
                50.0,
                50.02,
                50.01,
                REGULAR_NOW,
                source="marked-nav-test",
                avg_daily_volume_usd=500_000_000,
            )

    contract = _option_contract("marked-call", "2026-08-21", "call")
    pipeline = _restart_test_allocator(
        paper_root,
        now=REGULAR_NOW,
        discovery=MarkedDiscovery(REGULAR_NOW),
        option_data=_AllocatorMonitoringOptions(REGULAR_NOW),
    )
    pipeline.broker.store.save_account(Account(8_000, 10_000), REGULAR_NOW)
    pipeline.broker.store.save_positions(
        {"MSFT": Position("MSFT", 10, 100, REGULAR_NOW, REGULAR_NOW)}
    )
    pipeline.option_broker.store.save_positions(
        {
            contract.option_id: OptionPosition(
                contract,
                1,
                2.0,
                REGULAR_NOW,
                REGULAR_NOW,
            )
        }
    )

    state = pipeline._account_state(REGULAR_NOW)

    assert state["nav_usd"] == 8_600
    assert state["nav_valuation_method"] == "conservative_liquidation_bid_v1"
    assert state["equity_mark_times"] == {"MSFT": REGULAR_NOW}
    assert state["option_mark_times"] == {contract.option_id: REGULAR_NOW}


def test_allocator_live_monitor_uses_post_fetch_observation_cutoff(
    paper_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.core.models import Position
    from scripts.discovery import ai_instrument_allocator_pipeline as allocator_module

    stage_start = "2026-07-13T15:00:00+00:00"
    quote_time = "2026-07-13T15:00:03+00:00"
    post_fetch = "2026-07-13T15:00:05+00:00"
    pipeline = _restart_test_allocator(
        paper_root,
        discovery=_AllocatorExecutionDiscovery(quote_time),
    )
    pipeline.broker.store.save_positions(
        {"AAPL": Position("AAPL", 1, 100, stage_start, stage_start)}
    )
    pipeline.mandates.store.write_json(
        "position_mandates.json",
        {
            "equity:AAPL": _open_allocator_mandate(
                "equity:AAPL",
                horizon="next_close",
                max_holding_trading_days=1,
                opened_at=stage_start,
                planned_exit_at=NEXT_CLOSE_EXIT,
                planned_stop_price=97.0,
            )
        },
    )
    times = iter([stage_start, post_fetch])
    monkeypatch.setattr(allocator_module, "utc_now", lambda: next(times))

    result = pipeline.monitor_only()

    assert result["quote_errors"] == {}
    assert result["portfolio"]["asof"] == post_fetch
    assert result["portfolio"]["nav_calculated_at"] == post_fetch
    assert result["portfolio"]["equity_mark_times"] == {"AAPL": quote_time}
    assert set(pipeline.broker.store.positions()) == {"AAPL"}


def test_allocator_explicit_replay_rejects_post_cutoff_holding_mark(
    paper_root: Path,
) -> None:
    from scripts.core.models import Position

    decision_time = "2026-07-13T15:00:00+00:00"
    pipeline = _restart_test_allocator(
        paper_root,
        discovery=_AllocatorExecutionDiscovery("2026-07-13T15:00:03+00:00"),
    )
    pipeline.broker.store.save_positions(
        {"AAPL": Position("AAPL", 1, 100, decision_time, decision_time)}
    )

    with pytest.raises(ValueError, match="future"):
        pipeline._account_state(decision_time)


def test_allocator_account_state_rejects_equity_position_key_mismatch(
    paper_root: Path,
) -> None:
    from scripts.core.models import Position

    pipeline = _restart_test_allocator(paper_root, now=REGULAR_NOW)
    pipeline.broker.store.save_positions(
        {"MSFT": Position("AAPL", 1, 100, REGULAR_NOW, REGULAR_NOW)}
    )

    with pytest.raises(ValueError, match="position identity mismatch"):
        pipeline._account_state(REGULAR_NOW)


@pytest.mark.parametrize(
    "contract_changes",
    [
        {"option_id": "different-option"},
        {"underlying": ""},
        {"option_type": "straddle"},
    ],
)
def test_allocator_account_state_rejects_option_position_identity_corruption(
    paper_root: Path,
    contract_changes: dict,
) -> None:
    from scripts.options.models import OptionPosition

    option_id = "position-map-option"
    contract = replace(
        _option_contract(option_id, "2026-08-21", "call"),
        **contract_changes,
    )
    pipeline = _restart_test_allocator(
        paper_root,
        now=REGULAR_NOW,
        option_data=_AllocatorMonitoringOptions(REGULAR_NOW),
    )
    pipeline.option_broker.store.save_positions(
        {
            option_id: OptionPosition(
                contract,
                1,
                1.0,
                REGULAR_NOW,
                REGULAR_NOW,
            )
        }
    )

    with pytest.raises(ValueError, match="position identity mismatch"):
        pipeline._account_state(REGULAR_NOW)


def test_allocator_entry_fails_closed_when_existing_position_mark_is_stale(
    paper_root: Path,
) -> None:
    from scripts.core.models import Position, Quote

    class StaleHoldingDiscovery(_AllocatorExecutionDiscovery):
        def fetch_current_quote(self, symbol: str, **_kwargs):
            asof = "2026-07-13T13:00:00+00:00" if symbol == "MSFT" else REGULAR_NOW
            return Quote(
                symbol,
                100.0,
                100.01,
                100.005,
                asof,
                source="stale-mark-test",
                avg_daily_volume_usd=500_000_000,
            )

    pipeline = _restart_test_allocator(
        paper_root,
        now=REGULAR_NOW,
        discovery=StaleHoldingDiscovery(REGULAR_NOW),
    )
    pipeline.broker.store.save_positions(
        {"MSFT": Position("MSFT", 1, 100, REGULAR_NOW, REGULAR_NOW)}
    )
    plan = {
        "plan_id": "stale-mark-plan",
        "strategy": "ai_instrument_allocator_v1",
        "ticker": "AAPL",
        "stage": "intraday",
        "signal": _anchored_signal(),
        "snapshot": {"snapshot_id": "stale-mark-snapshot"},
    }

    result = pipeline._execute_plan(plan, REGULAR_NOW, stage="intraday")

    assert result["status"] == "no_trade"
    assert "marked NAV unavailable" in result["reason"]
    assert pipeline.broker.store.orders() == {}
    assert pipeline.option_broker.store.orders() == {}


def test_allocator_equity_risk_gate_applies_supplied_marked_nav(
    paper_root: Path,
) -> None:
    from scripts.core.models import Account, Order, Position
    from scripts.risk.risk_gate import check_order

    config = load_runtime_config(paper_root)
    order = Order(
        order_id="marked-nav-order",
        decision_id="marked-nav-order",
        symbol="AAPL",
        side="buy",
        order_type="limit",
        quantity=20,
        limit_price=100.01,
        quote_seen_at=REGULAR_NOW,
        created_at=REGULAR_NOW,
        strategy="ai_instrument_allocator_v1",
        planned_stop_price=99.0,
        signal_horizon="next_close",
    )

    decision = check_order(
        order,
        _underlying_quote(),
        Account(5_000, 10_000),
        {"MSFT": Position("MSFT", 50, 100, REGULAR_NOW, REGULAR_NOW)},
        {},
        {"trades": 0},
        config,
        REGULAR_NOW,
        option_positions={},
        option_orders={},
        entry_nav_usd=6_000,
    )

    assert decision.reason == "max order size exceeded"


def test_allocator_option_risk_gate_applies_supplied_marked_nav(
    paper_root: Path,
) -> None:
    from scripts.core.models import Account
    from scripts.options.models import OptionOrder
    from scripts.options.risk_gate import check_option_order

    contract = _option_contract("marked-nav-put", "2026-08-21", "put")
    order = OptionOrder(
        order_id="marked-nav-option",
        decision_id="marked-nav-option",
        contract=contract,
        intent="buy_to_open",
        quantity=1,
        order_type="limit",
        limit_price=2.0,
        quote_seen_at=REGULAR_NOW,
        created_at=REGULAR_NOW,
        strategy="ai_instrument_allocator_v1",
        signal_horizon="next_close",
    )

    decision = check_option_order(
        order,
        _option_quote(contract.option_id, bid=1.99, ask=2.0),
        Account(10_000, 10_000),
        {},
        {},
        {},
        {},
        {"trades": 0},
        load_runtime_config(paper_root),
        REGULAR_NOW,
        entry_nav_usd=5_000,
    )

    assert decision.reason == "max option premium risk exceeded"
