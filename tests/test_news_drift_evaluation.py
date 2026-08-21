from __future__ import annotations

import json
from copy import deepcopy

from scripts.core.config import load_runtime_config
from scripts.llm.mock_provider import MockProvider
from scripts.llm.usage_tracker import UsageTracker
from scripts.evaluation.evaluate_news_drift import calculate_news_drift_metrics
from scripts.news_drift.pipeline import NewsDriftPipeline
from tests.test_news_drift_pipeline import FakeDiscovery, FakeNews, NOW, _event


def test_news_drift_metrics_keep_event_firm_and_portfolio_day_separate(tmp_path):
    config = deepcopy(load_runtime_config())
    config["strategies"]["llm_news_drift_v1"]["minimum_event_labels_for_evaluation"] = 1
    config["strategies"]["llm_news_drift_v1"]["minimum_portfolio_days_for_evaluation"] = 1
    tracker = UsageTracker(tmp_path / "logs" / "llm_usage.jsonl")
    discovery = FakeDiscovery()
    pipeline = NewsDriftPipeline(
        tmp_path,
        config,
        MockProvider(tracker),
        tracker,
        discovery_adapter=discovery,
        news_adapter=FakeNews([_event()]),
    )
    proposal = pipeline.run(NOW)["proposals"][0]
    target = proposal["label_targets"]["plus_1m"]
    discovery.quote_asof = target
    pipeline.resolve_due_labels(target)

    metrics = calculate_news_drift_metrics(tmp_path)

    assert metrics["event_count"] == 1
    assert metrics["proposal_count"] == 1
    assert metrics["valid_return_label_count"] == 1
    assert metrics["firm_day_count"] == 1
    assert metrics["portfolio_day_count"] == 1
    assert metrics["horizons"]["plus_1m"]["event_level"]["count"] == 1
    assert metrics["horizons"]["plus_1m"]["observed_round_trip_cost_bps"] > 0
    assert metrics["api_usage"]["estimated_exa_cost_usd"] is None
    assert metrics["promotion_eligible"] is False


def test_news_drift_metrics_fail_cleanly_before_first_cycle(tmp_path):
    metrics = calculate_news_drift_metrics(tmp_path)
    assert metrics["status"] == "no_forward_data"
    assert metrics["profitability"] == "insufficient_forward_evidence"
