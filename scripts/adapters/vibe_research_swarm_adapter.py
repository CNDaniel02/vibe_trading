from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from scripts.adapters.errors import AdapterConfigurationError
from scripts.adapters.vibe_runtime import VibeRuntime
from scripts.core.models import utc_now


class VibeResearchSwarmAdapter:
    """Read-only Vibe Swarm sidecar. Reports never enter the order path."""

    def __init__(self, project_root: str | Path, vibe_config: dict[str, Any]) -> None:
        self.project_root = Path(project_root).resolve()
        self.config = vibe_config.get("research_swarm", {})
        self.runtime = VibeRuntime(self.project_root, vibe_config)

    def inspect(self) -> dict[str, Any]:
        return self.runtime.bridge(
            "inspect_swarm",
            {
                "preset_file": str(self._preset_path()),
                "allowed_tools": list(self.config.get("allowed_tools", [])),
            },
        )

    def run(self, tickers: list[str], data_cutoff_time: str, goal: str) -> dict[str, Any]:
        if not self.config.get("enabled", False):
            raise AdapterConfigurationError("Vibe research swarm is disabled")
        inspection = self.inspect()
        if inspection.get("errors"):
            raise AdapterConfigurationError("Vibe research swarm preset failed inspection")
        run_root = self._resolve_project_path(str(self.config.get("run_root", "logs/vibe_research")))
        result = self.runtime.bridge(
            "run_swarm",
            {
                "preset_file": str(self._preset_path()),
                "allowed_tools": list(self.config.get("allowed_tools", [])),
                "run_root": str(run_root),
                "timeout_seconds": int(self.config.get("timeout_seconds", 1800)),
                "variables": {"tickers": ",".join(tickers), "data_cutoff_time": data_cutoff_time, "goal": goal},
            },
        )
        report_record = {
            "ts": utc_now(),
            "data_cutoff_time": data_cutoff_time,
            "tickers": list(tickers),
            "research_only": True,
            "may_create_orders": False,
            "vibe_result": result,
        }
        output = run_root / f"{result['run_id']}.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report_record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return {**result, "report_record": str(output)}

    def _preset_path(self) -> Path:
        path = self._resolve_project_path(str(self.config.get("preset_file", "")))
        if not path.is_file():
            raise AdapterConfigurationError("Vibe research preset file not found")
        return path

    def _resolve_project_path(self, value: str) -> Path:
        path = Path(value)
        resolved = path.resolve() if path.is_absolute() else (self.project_root / path).resolve()
        try:
            resolved.relative_to(self.project_root)
        except ValueError as exc:
            raise AdapterConfigurationError("research paths must stay inside the project root") from exc
        return resolved
