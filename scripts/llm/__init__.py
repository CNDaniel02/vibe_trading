"""Provider-neutral LLM boundary for shadow research agents."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from scripts.llm.api_provider import ApiProvider
from scripts.llm.base_provider import LLMProvider
from scripts.llm.local_provider import LocalProvider
from scripts.llm.mock_provider import MockProvider
from scripts.llm.usage_tracker import UsageTracker


def build_provider(config: dict[str, Any], root: str | Path) -> tuple[LLMProvider, UsageTracker]:
    provider_name = str(config.get("provider", "mock"))
    usage_path = Path(root) / str(config.get("usage_log", "logs/llm_usage.jsonl"))
    tracker = UsageTracker(usage_path)
    if provider_name == "mock":
        return MockProvider(tracker), tracker
    if provider_name == "api":
        return ApiProvider(config.get("api", {}), tracker), tracker
    if provider_name == "local":
        return LocalProvider(str(config.get("local", {}).get("model", "not-configured")), tracker), tracker
    raise ValueError(f"unsupported LLM provider: {provider_name}")
