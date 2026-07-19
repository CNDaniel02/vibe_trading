from __future__ import annotations

from scripts.llm.base_provider import LLMProvider, ProviderError, ProviderRequest, ProviderResponse
from scripts.llm.usage_tracker import UsageTracker


class LocalProvider(LLMProvider):
    def __init__(self, model: str = "not-configured", tracker: UsageTracker | None = None) -> None:
        self.model = model
        self.tracker = tracker or UsageTracker()

    def generate(self, request: ProviderRequest) -> ProviderResponse:
        self.tracker.record(
            snapshot_id=str(request.input_payload.get("snapshot_id", "unknown")),
            agent_name=request.agent_name,
            provider="local",
            model=self.model,
            prompt_version=request.prompt_version,
            error="local model runtime not deployed",
        )
        raise ProviderError("local provider interface exists but no local model runtime is deployed")
