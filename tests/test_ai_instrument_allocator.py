from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.core.config import load_runtime_config
from scripts.discovery.ai_gated_pipeline import AiGatedPaperPipeline
from scripts.llm.mock_provider import MockProvider
from scripts.llm.usage_tracker import UsageTracker
from scripts.options.paper_broker import OptionPaperBroker
from scripts.orchestrator import forward_paper_service as forward_service_module
from scripts.simulation.paper_broker import PaperBroker


REGULAR_NOW = "2026-07-13T15:00:00+00:00"


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
