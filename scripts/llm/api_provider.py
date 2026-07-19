from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

from scripts.llm.base_provider import LLMProvider, ProviderError, ProviderRequest, ProviderResponse, ProviderUsage
from scripts.llm.schemas import schema_for_provider, validate_schema
from scripts.llm.usage_tracker import UsageTracker


class ApiProvider(LLMProvider):
    """OpenAI-compatible chat-completions provider with strict JSON Schema output."""

    def __init__(self, config: dict[str, Any], tracker: UsageTracker | None = None) -> None:
        self.config = config
        self.tracker = tracker or UsageTracker()
        self.model = str(os.getenv("LLM_MODEL") or config.get("model") or "")
        self.base_url = str(config.get("base_url", "")).rstrip("/")
        self.endpoint = str(config.get("endpoint", "/chat/completions"))
        self.api_key_env = str(config.get("api_key_env", "LLM_API_KEY"))
        self.timeout = float(config.get("timeout_seconds", 45))
        self.max_retries = int(config.get("max_retries", 2))

    def generate(self, request: ProviderRequest) -> ProviderResponse:
        api_key = os.getenv(self.api_key_env)
        if not api_key:
            self.tracker.record(
                snapshot_id=str(request.input_payload.get("snapshot_id", "unknown")),
                agent_name=request.agent_name,
                provider="api",
                model=self.model or "not-configured",
                prompt_version=request.prompt_version,
                error=f"missing API key environment variable: {self.api_key_env}",
            )
            raise ProviderError(f"missing API key environment variable: {self.api_key_env}")
        if not self.model or not self.base_url:
            self.tracker.record(
                snapshot_id=str(request.input_payload.get("snapshot_id", "unknown")),
                agent_name=request.agent_name,
                provider="api",
                model=self.model or "not-configured",
                prompt_version=request.prompt_version,
                error="API model or base_url not configured",
            )
            raise ProviderError("API model and base_url must be configured")

        body = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": request.system_prompt},
                {"role": "user", "content": json.dumps(request.input_payload, sort_keys=True)},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": request.schema_name,
                    "strict": True,
                    "schema": schema_for_provider(request.output_schema),
                },
            },
        }
        encoded = json.dumps(body).encode("utf-8")
        url = self.base_url + self.endpoint
        started = time.perf_counter()
        error: Exception | None = None
        retries = 0
        for attempt in range(self.max_retries + 1):
            retries = attempt
            try:
                http_request = urllib.request.Request(
                    url,
                    data=encoded,
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(http_request, timeout=self.timeout) as response:
                    raw = json.loads(response.read().decode("utf-8"))
                content = raw["choices"][0]["message"]["content"]
                data = json.loads(content)
                validate_schema(data, request.output_schema)
                usage_raw = raw.get("usage", {})
                input_tokens = int(usage_raw.get("prompt_tokens", 0))
                output_tokens = int(usage_raw.get("completion_tokens", 0))
                input_rate = self.config.get("input_cost_per_million_usd")
                output_rate = self.config.get("output_cost_per_million_usd")
                cost = None
                if input_rate is not None and output_rate is not None:
                    cost = (input_tokens * float(input_rate) + output_tokens * float(output_rate)) / 1_000_000
                latency_ms = (time.perf_counter() - started) * 1000
                usage = ProviderUsage(input_tokens, output_tokens, latency_ms, cost, retries)
                self.tracker.record(
                    snapshot_id=str(request.input_payload["snapshot_id"]),
                    agent_name=request.agent_name,
                    provider="api",
                    model=self.model,
                    prompt_version=request.prompt_version,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    latency_ms=latency_ms,
                    estimated_cost_usd=cost,
                    retries=retries,
                )
                return ProviderResponse(data, self.model, "api", usage, raw.get("id"))
            except (urllib.error.URLError, urllib.error.HTTPError, KeyError, ValueError, json.JSONDecodeError) as exc:
                error = exc
                if attempt < self.max_retries:
                    time.sleep(min(2**attempt, 4))

        latency_ms = (time.perf_counter() - started) * 1000
        safe_error = f"{type(error).__name__}: API request or structured-output validation failed"
        self.tracker.record(
            snapshot_id=str(request.input_payload.get("snapshot_id", "unknown")),
            agent_name=request.agent_name,
            provider="api",
            model=self.model,
            prompt_version=request.prompt_version,
            latency_ms=latency_ms,
            retries=retries,
            error=safe_error,
        )
        raise ProviderError(safe_error) from error
