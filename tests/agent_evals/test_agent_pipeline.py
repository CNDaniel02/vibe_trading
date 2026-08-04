from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.agents.api_investment_team import ApiInvestmentTeam
from scripts.core.config import load_runtime_config
from scripts.evaluation.evaluate_agents import DEFAULT_FIXTURES, agent_input, evaluate, load_snapshots
from scripts.llm.api_provider import ApiProvider
from scripts.llm.base_provider import ProviderError, ProviderRequest
from scripts.llm.mock_provider import MockProvider
from scripts.llm.schemas import NEWS_OUTPUT_SCHEMA
from scripts.llm.usage_tracker import UsageTracker


def make_team(paper_root: Path):
    config = load_runtime_config(paper_root)
    tracker = UsageTracker()
    return ApiInvestmentTeam(paper_root, config, MockProvider(tracker), tracker), tracker


def test_fixture_pack_contains_at_least_twenty_snapshots():
    assert len(load_snapshots(DEFAULT_FIXTURES)) >= 20


def test_mock_eval_meets_core_quality_gates(paper_root):
    report = evaluate(paper_root, DEFAULT_FIXTURES, "mock")
    assert report["scores"]["schema_validity"] == 1.0
    assert report["scores"]["timestamp_correctness"] == 1.0
    assert report["scores"]["source_grounding"] == 1.0
    assert report["scores"]["hallucination_rate"] == 0.0
    assert report["scores"]["no_trade_correctness"] == 1.0
    assert report["scores"]["challenge_usefulness"] == 1.0
    assert report["scores"]["decision_consistency"] == 1.0
    assert report["scores"]["risk_rule_compliance"] == 1.0


def test_full_market_rejection_uses_zero_model_calls(paper_root):
    snapshot = next(item for item in load_snapshots(DEFAULT_FIXTURES) if item["snapshot_id"] == "wide_spread_08")
    team, tracker = make_team(paper_root)
    result = team.run(agent_input(snapshot))
    assert result.action == "no_trade"
    assert result.model_calls == 0
    assert tracker.summary()["calls"] == 0


def test_candidate_uses_exactly_three_model_calls(paper_root):
    snapshot = next(item for item in load_snapshots(DEFAULT_FIXTURES) if item["snapshot_id"] == "positive_earnings_01")
    team, tracker = make_team(paper_root)
    result = team.run(agent_input(snapshot))
    assert result.action == "buy"
    assert result.model_calls == 3
    assert tracker.summary()["calls"] == 3


def test_no_news_stops_after_news_agent(paper_root):
    snapshot = next(item for item in load_snapshots(DEFAULT_FIXTURES) if item["snapshot_id"] == "no_news_06")
    team, tracker = make_team(paper_root)
    result = team.run(agent_input(snapshot))
    assert result.action == "no_trade"
    assert result.model_calls == 1
    assert tracker.summary()["calls"] == 1


def test_future_news_fails_closed_before_model_call(paper_root):
    snapshot = next(item for item in load_snapshots(DEFAULT_FIXTURES) if item["snapshot_id"] == "future_news_22")
    team, tracker = make_team(paper_root)
    result = team.run(agent_input(snapshot))
    assert result.fail_closed
    assert result.model_calls == 0
    assert tracker.summary()["calls"] == 0


def test_deterministic_risk_gate_has_final_veto(paper_root):
    snapshot = next(item for item in load_snapshots(DEFAULT_FIXTURES) if item["snapshot_id"] == "risk_gate_position_limit_23")
    team, _ = make_team(paper_root)
    result = team.run(agent_input(snapshot))
    assert result.action == "no_trade"
    assert result.model_calls == 3
    assert not result.risk_approved
    assert result.risk_reason == "max order size exceeded"
    assert "deterministic risk gate vetoed model decision" in result.guardrail_actions


def test_shadow_pipeline_does_not_write_baseline_orders(paper_root):
    before = (paper_root / "state" / "paper_orders.json").read_text(encoding="utf-8")
    snapshot = load_snapshots(DEFAULT_FIXTURES)[0]
    team, _ = make_team(paper_root)
    assert team.run(agent_input(snapshot)).action == "buy"
    after = (paper_root / "state" / "paper_orders.json").read_text(encoding="utf-8")
    assert before == after


def test_api_provider_requires_environment_key(monkeypatch):
    monkeypatch.delenv("TEST_LLM_API_KEY", raising=False)
    tracker = UsageTracker()
    provider = ApiProvider({"base_url": "https://example.invalid/v1", "model": "test", "api_key_env": "TEST_LLM_API_KEY"}, tracker)
    request = ProviderRequest("news_agent", "v1", "test", agent_input(load_snapshots(DEFAULT_FIXTURES)[0]), NEWS_OUTPUT_SCHEMA, "news_v1")
    with pytest.raises(ProviderError, match="environment variable"):
        provider.generate(request)
    assert tracker.summary()["calls"] == 1
    assert tracker.summary()["errors"] == 1


def test_api_provider_sends_strict_schema_and_tracks_usage(monkeypatch):
    monkeypatch.setenv("TEST_LLM_API_KEY", "not-a-real-key")
    snapshot = agent_input(load_snapshots(DEFAULT_FIXTURES)[0])
    mock = MockProvider(UsageTracker())
    request = ProviderRequest("news_agent", "v1", "test", snapshot, NEWS_OUTPUT_SCHEMA, "news_v1")
    structured = mock.generate(request).data
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(
                {
                    "id": "fake-response",
                    "choices": [{"message": {"content": json.dumps(structured)}}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 40},
                }
            ).encode("utf-8")

    def fake_urlopen(http_request, timeout):
        captured["body"] = json.loads(http_request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    tracker = UsageTracker()
    provider = ApiProvider(
        {
            "base_url": "https://example.invalid/v1",
            "endpoint": "/chat/completions",
            "model": "test-model",
            "api_key_env": "TEST_LLM_API_KEY",
            "input_cost_per_million_usd": 1.0,
            "output_cost_per_million_usd": 2.0,
        },
        tracker,
    )
    response = provider.generate(request)
    assert response.data == structured
    assert captured["body"]["response_format"]["json_schema"]["strict"] is True
    assert captured["body"]["response_format"]["json_schema"]["schema"]["additionalProperties"] is False
    assert tracker.summary()["calls"] == 1
    assert tracker.summary()["input_tokens"] == 100
    assert tracker.summary()["output_tokens"] == 40
    assert tracker.summary()["estimated_cost_usd"] == pytest.approx(0.00018)


def test_api_provider_supports_deepseek_json_mode_and_base_url_override(monkeypatch):
    monkeypatch.setenv("TEST_LLM_API_KEY", "not-a-real-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://deepseek.example")
    snapshot = agent_input(load_snapshots(DEFAULT_FIXTURES)[0])
    request = ProviderRequest("news_agent", "v1", "test", snapshot, NEWS_OUTPUT_SCHEMA, "news_v1")
    structured = MockProvider(UsageTracker()).generate(request).data
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(
                {
                    "id": "fake-response",
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"content": f"```json\\n{json.dumps(structured)}\\n```"},
                        }
                    ],
                }
            ).encode("utf-8")

    def fake_urlopen(http_request, timeout):
        captured["url"] = http_request.full_url
        captured["body"] = json.loads(http_request.data.decode("utf-8"))
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    provider = ApiProvider(
        {
            "base_url": "https://ignored.example",
            "base_url_env": "OPENAI_BASE_URL",
            "endpoint": "/chat/completions",
            "model": "deepseek-v4-pro",
            "api_key_env": "TEST_LLM_API_KEY",
            "response_format": "json_object",
            "max_tokens": 4096,
            "thinking": {"type": "disabled"},
        },
        UsageTracker(),
    )
    assert provider.generate(request).data == structured
    assert captured["url"] == "https://deepseek.example/chat/completions"
    assert captured["body"]["response_format"] == {"type": "json_object"}
    assert captured["body"]["max_tokens"] == 4096
    assert captured["body"]["thinking"] == {"type": "disabled"}
    assert "JSON Schema" in captured["body"]["messages"][0]["content"]


def test_api_provider_empty_structured_content_is_diagnostic_but_safe(monkeypatch):
    monkeypatch.setenv("TEST_LLM_API_KEY", "not-a-real-key")
    snapshot = agent_input(load_snapshots(DEFAULT_FIXTURES)[0])
    request = ProviderRequest("news_agent", "v1", "test", snapshot, NEWS_OUTPUT_SCHEMA, "news_v1")

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(
                {
                    "choices": [
                        {
                            "finish_reason": "length",
                            "message": {"content": "", "reasoning_content": "internal reasoning"},
                        }
                    ]
                }
            ).encode("utf-8")

    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: FakeResponse())
    tracker = UsageTracker()
    provider = ApiProvider(
        {"base_url": "https://example.invalid", "model": "test", "api_key_env": "TEST_LLM_API_KEY", "max_retries": 0},
        tracker,
    )
    with pytest.raises(ProviderError, match="empty assistant content .*finish_reason=length"):
        provider.generate(request)
    assert "internal reasoning" not in str(tracker.records[-1].error)


def test_api_provider_selects_thinking_mode_by_agent(monkeypatch):
    monkeypatch.setenv("TEST_LLM_API_KEY", "not-a-real-key")
    snapshot = agent_input(load_snapshots(DEFAULT_FIXTURES)[0])
    structured = MockProvider(UsageTracker()).generate(
        ProviderRequest("news_agent", "v1", "test", snapshot, NEWS_OUTPUT_SCHEMA, "news_v1")
    ).data
    captured = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": json.dumps(structured)}}]}).encode("utf-8")

    def fake_urlopen(http_request, timeout):
        assert timeout > 0
        captured.append(json.loads(http_request.data.decode("utf-8")))
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    provider = ApiProvider(
        {
            "base_url": "https://example.invalid",
            "model": "test",
            "api_key_env": "TEST_LLM_API_KEY",
            "thinking": {
                "default": {"type": "disabled"},
                "agents": {"challenge_agent": {"type": "enabled"}},
            },
        },
        UsageTracker(),
    )
    assert provider.generate(ProviderRequest("news_agent", "v1", "test", snapshot, NEWS_OUTPUT_SCHEMA, "news_v1")).data == structured
    assert provider.generate(ProviderRequest("challenge_agent", "v1", "test", snapshot, NEWS_OUTPUT_SCHEMA, "challenge_v1")).data == structured
    assert captured[0]["thinking"] == {"type": "disabled"}
    assert captured[1]["thinking"] == {"type": "enabled"}


def test_team_compacts_only_llm_news_excerpts(paper_root):
    team, _ = make_team(paper_root)
    payload = {
        "snapshot_id": "compact-test",
        "available_news": [
            {
                "headline": "Headline",
                "published_at": "2026-07-01T00:00:00Z",
                "highlights": ["a" * 1000, "b" * 1000, "c" * 1000],
                "irrelevant": "drop",
            }
        ],
    }
    compacted = team._compact_provider_payload(payload)
    assert payload["available_news"][0]["highlights"][0] == "a" * 1000
    assert "irrelevant" not in compacted["available_news"][0]
    assert compacted["available_news"][0]["highlights"] == ["a" * 600, "b" * 600]


def test_strategy_comparison_is_same_snapshot_and_shadow_only(paper_root):
    report = evaluate(paper_root, DEFAULT_FIXTURES, "mock")
    assert report["comparison"]["baseline"]["strategy"] == "relative_strength_v1"
    assert report["comparison"]["shadow"]["strategy"] == "multi_agent_relative_strength_v2_candidate"
    assert report["comparison"]["shadow"]["rule_violations"] == 0
    assert 0 <= report["comparison"]["decision_agreement"] <= 1


def test_prompts_contain_no_broker_tool_instruction():
    prompt_dir = Path(__file__).resolve().parents[2] / "scripts" / "llm" / "prompts"
    prompts = "\n".join(path.read_text(encoding="utf-8") for path in prompt_dir.glob("*.md"))
    assert "place_equity_order" not in prompts
    assert "place_option_order" not in prompts
    assert "Robinhood" not in prompts
