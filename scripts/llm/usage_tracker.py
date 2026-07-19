from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from scripts.core.models import utc_now


@dataclass(frozen=True)
class UsageRecord:
    ts: str
    snapshot_id: str
    agent_name: str
    provider: str
    model: str
    prompt_version: str
    input_tokens: int
    output_tokens: int
    latency_ms: float
    estimated_cost_usd: float | None
    retries: int
    error: str | None


class UsageTracker:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else None
        self.records: list[UsageRecord] = []

    def record(
        self,
        *,
        snapshot_id: str,
        agent_name: str,
        provider: str,
        model: str,
        prompt_version: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        latency_ms: float = 0.0,
        estimated_cost_usd: float | None = 0.0,
        retries: int = 0,
        error: str | None = None,
    ) -> UsageRecord:
        record = UsageRecord(
            ts=utc_now(),
            snapshot_id=snapshot_id,
            agent_name=agent_name,
            provider=provider,
            model=model,
            prompt_version=prompt_version,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=round(latency_ms, 3),
            estimated_cost_usd=round(estimated_cost_usd, 8) if estimated_cost_usd is not None else None,
            retries=retries,
            error=error,
        )
        self.records.append(record)
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(asdict(record), sort_keys=True) + "\n")
        return record

    def summary(self) -> dict[str, Any]:
        return {
            "calls": len(self.records),
            "input_tokens": sum(item.input_tokens for item in self.records),
            "output_tokens": sum(item.output_tokens for item in self.records),
            "latency_ms": round(sum(item.latency_ms for item in self.records), 3),
            "estimated_cost_usd": round(sum(item.estimated_cost_usd or 0 for item in self.records), 8),
            "unpriced_calls": sum(1 for item in self.records if item.estimated_cost_usd is None),
            "errors": sum(1 for item in self.records if item.error),
            "retries": sum(item.retries for item in self.records),
        }
