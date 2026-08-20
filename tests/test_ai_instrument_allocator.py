from __future__ import annotations

from pathlib import Path
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


def test_entry_frozen_ai_pipeline_skips_research_but_keeps_monitor_result(
    paper_root: Path,
) -> None:
    config = load_runtime_config(paper_root)
    tracker = UsageTracker()
    pipeline = AiGatedPaperPipeline(
        paper_root,
        config,
        MockProvider(tracker),
        tracker,
        discovery_adapter=_MustNotDiscover(),
        news_adapter=_NoNews(),
        option_data=_NoOptions(),
    )

    result = pipeline.run(REGULAR_NOW)

    assert result["event"] == "ai_gated_entries_frozen"
    assert result["paper_orders_created"] == 0
    assert result["model_calls"] == 0
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
    assert summary["conservative_move_pct"] == 0.5
    assert summary["probability_status"] == "uncalibrated"


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
