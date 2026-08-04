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


class StructuredContentError(ValueError):
    """A safe, actionable failure to obtain a JSON object from the model."""


class ApiProvider(LLMProvider):
    """OpenAI-compatible chat-completions provider with strict JSON Schema output."""

    def __init__(self, config: dict[str, Any], tracker: UsageTracker | None = None) -> None:
        self.config = config
        self.tracker = tracker or UsageTracker()
        self.model = str(os.getenv("LLM_MODEL") or config.get("model") or "")
        self.base_url = str(os.getenv(str(config.get("base_url_env", "OPENAI_BASE_URL"))) or config.get("base_url", "")).rstrip("/")
        self.endpoint = str(config.get("endpoint", "/chat/completions"))
        self.api_key_env = str(config.get("api_key_env", "LLM_API_KEY"))
        self.timeout = float(config.get("timeout_seconds", 45))
        self.max_retries = int(config.get("max_retries", 2))
        self.max_tokens = config.get("max_tokens")
        self.response_format = str(config.get("response_format", "json_schema"))
        thinking = config.get("thinking")
        self.default_thinking: dict[str, Any] | None = None
        self.agent_thinking: dict[str, dict[str, Any]] = {}
        if isinstance(thinking, dict) and ("default" in thinking or "agents" in thinking):
            default = thinking.get("default")
            self.default_thinking = dict(default) if isinstance(default, dict) else None
            agent_overrides = thinking.get("agents", {})
            if isinstance(agent_overrides, dict):
                self.agent_thinking = {
                    str(name): dict(value)
                    for name, value in agent_overrides.items()
                    if isinstance(value, dict)
                }
        elif isinstance(thinking, dict):
            # Backwards-compatible single policy for all agents.
            self.default_thinking = dict(thinking)

    def _thinking_for(self, agent_name: str) -> dict[str, Any] | None:
        configured = self.agent_thinking.get(agent_name, self.default_thinking)
        return dict(configured) if configured is not None else None

    @staticmethod
    def _parse_structured_content(choice: dict[str, Any]) -> dict[str, Any]:
        """Parse a model JSON object without ever logging its raw response text."""
        message = choice.get("message")
        if not isinstance(message, dict):
            raise StructuredContentError("response choice did not contain a message object")
        content = message.get("content")
        finish_reason = choice.get("finish_reason", "unknown")
        reasoning_present = bool(message.get("reasoning_content"))
        if not isinstance(content, str) or not content.strip():
            raise StructuredContentError(
                "empty assistant content "
                f"(finish_reason={finish_reason}, reasoning_present={reasoning_present})"
            )

        candidate = content.strip().lstrip("\ufeff")
        if candidate.startswith("```"):
            first_newline = candidate.find("\n")
            if first_newline != -1:
                candidate = candidate[first_newline + 1 :]
            if candidate.rstrip().endswith("```"):
                candidate = candidate.rstrip()[:-3].rstrip()
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError as exc:
            # Providers occasionally wrap an otherwise valid object in prose.
            # Accept only the outermost object; local schema validation remains
            # the non-negotiable boundary below.
            start = candidate.find("{")
            end = candidate.rfind("}")
            if start == -1 or end <= start:
                raise StructuredContentError("assistant content did not contain a JSON object") from exc
            try:
                data = json.loads(candidate[start : end + 1])
            except json.JSONDecodeError as nested_exc:
                raise StructuredContentError("assistant JSON object could not be parsed") from nested_exc
        if not isinstance(data, dict):
            raise StructuredContentError("assistant content was not a JSON object")
        return data

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

        system_prompt = request.system_prompt
        response_format: dict[str, Any]
        if self.response_format == "json_schema":
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": request.schema_name,
                    "strict": True,
                    "schema": schema_for_provider(request.output_schema),
                },
            }
        elif self.response_format == "json_object":
            # DeepSeek's documented structured-output mode guarantees JSON syntax,
            # not server-side JSON Schema enforcement. Keep the schema in the prompt
            # and retain local Draft 2020-12 validation below as the hard boundary.
            system_prompt = (
                f"{system_prompt}\n\n"
                "Return only one JSON object. It must conform exactly to this JSON Schema; "
                "do not add markdown, commentary, or additional properties:\n"
                f"{json.dumps(schema_for_provider(request.output_schema), sort_keys=True)}"
            )
            response_format = {"type": "json_object"}
        else:
            raise ProviderError(f"unsupported response_format: {self.response_format}")

        body: dict[str, Any] = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(request.input_payload, sort_keys=True)},
            ],
            "response_format": response_format,
        }
        if self.max_tokens is not None:
            body["max_tokens"] = int(self.max_tokens)
        thinking = self._thinking_for(request.agent_name)
        if thinking is not None:
            body["thinking"] = thinking
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
                data = self._parse_structured_content(raw["choices"][0])
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
            except (urllib.error.URLError, urllib.error.HTTPError, KeyError, ValueError) as exc:
                error = exc
                if attempt < self.max_retries:
                    time.sleep(min(2**attempt, 4))

        latency_ms = (time.perf_counter() - started) * 1000
        if isinstance(error, StructuredContentError):
            safe_error = f"{type(error).__name__}: {error}"
        else:
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
