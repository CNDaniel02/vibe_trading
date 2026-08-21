from __future__ import annotations

from scripts.evaluation.run_news_drift_api_pilot import run_pilot
from scripts.llm.mock_provider import MockProvider
from scripts.llm.usage_tracker import UsageTracker


def test_news_drift_api_pilot_is_one_call_and_order_free():
    tracker = UsageTracker()
    result = run_pilot(".", provider=MockProvider(tracker), tracker=tracker)
    assert result["model_calls"] == 1
    assert len(result["signals"]) == 2
    assert result["market_data_calls"] == 0
    assert result["paper_orders_created"] == 0
    assert result["live_order_tools_called"] is False
