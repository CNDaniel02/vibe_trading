from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ProviderRequest:
    agent_name: str
    prompt_version: str
    system_prompt: str
    input_payload: dict[str, Any]
    output_schema: dict[str, Any]
    schema_name: str


@dataclass(frozen=True)
class ProviderUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    estimated_cost_usd: float | None = 0.0
    retries: int = 0


@dataclass(frozen=True)
class ProviderResponse:
    data: dict[str, Any]
    model: str
    provider: str
    usage: ProviderUsage = field(default_factory=ProviderUsage)
    response_id: str | None = None


class ProviderError(RuntimeError):
    pass


class LLMProvider(ABC):
    @abstractmethod
    def generate(self, request: ProviderRequest) -> ProviderResponse:
        raise NotImplementedError
