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
        horizon_days=1,
        move_pct=3.0,
        iv_shifts=[-0.05, 0.0, 0.05],
        costs=costs,
    )
    flat_later = reprice_option_scenarios(
        contract,
        quote,
        spot=100,
        now=REGULAR_NOW,
        horizon_days=5,
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
    )

    assert allocation["status"] == "selected"
    assert allocation["selected_instrument"]["instrument_type"] == "equity"
    assert allocation["selected_instrument"]["ticker"] == "AAPL"
    assert allocation["probability_ev_available"] is False
    assert allocation["probability_ev_usd"] is None
    assert allocation["raw_probability_used_for_ev"] is False


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
    )

    assert allocation["status"] == "no_trade"
    assert allocation["reason"] == "missing or invalid forecast reference"
    assert allocation["considered"] == []


def test_allocator_records_market_implied_move_comparison(
    paper_root: Path,
) -> None:
    import math

    from scripts.decision.instrument_allocator import allocate_instrument

    allocation = allocate_instrument(
        _anchored_signal(),
        _underlying_quote(),
        [(_option_contract("aapl-call", "2026-08-21"), _option_quote("aapl-call"))],
        _account_state(),
        load_runtime_config(paper_root),
        REGULAR_NOW,
    )

    implied = 0.30 * math.sqrt(1 / 365) * 100
    assert allocation["market_implied_move_pct"] == pytest.approx(implied)
    assert allocation["forecast_to_implied_move_ratio"] == pytest.approx(
        abs(allocation["remaining_move_pct"]) / implied
    )
    assert allocation["market_implied_move_method"] == "nearest_candidate_iv_sqrt_t"


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
    assert restarted_mandates.for_exposure("equity:AAPL")["horizon"] == "next_close"
    assert (paper_root / "state" / "paper_account.json").read_bytes() == legacy_account_before


def test_position_mandate_exit_is_horizon_aware_and_fails_closed() -> None:
    from scripts.exit.position_mandates import evaluate_mandate_exit

    mandate = {
        "exposure_id": "equity:AAPL",
        "status": "open",
        "ticker": "AAPL",
        "instrument_type": "equity",
        "horizon": "next_close",
        "planned_exit_at": "2026-07-14T19:50:00+00:00",
        "thesis_valid_until": "2026-07-14T19:50:00+00:00",
        "invalidation_triggered": False,
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


def test_allocator_execution_advances_to_latest_observed_option_quote(
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

    assert execution["status"] == "filled"
    assert execution["order"]["updated_at"] == "2026-07-13T13:32:05+00:00"
    assert execution["allocation"]["decision_time"] == "2026-07-13T13:32:05+00:00"
    assert execution["allocation"]["data_cutoff_time"] == "2026-07-13T13:32:05+00:00"
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
