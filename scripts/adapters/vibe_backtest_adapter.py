from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path
from typing import Any

from scripts.adapters.errors import AdapterConfigurationError, AdapterDataError
from scripts.adapters.vibe_market_data_adapter import VibeMarketDataAdapter
from scripts.adapters.vibe_runtime import VibeRuntime


class VibeBacktestAdapter:
    """Runs Vibe's independent research backtest without touching paper state."""

    def __init__(self, project_root: str | Path, vibe_config: dict[str, Any]) -> None:
        self.project_root = Path(project_root).resolve()
        self.vibe_config = vibe_config
        self.config = vibe_config.get("backtest", {})
        self.runtime = VibeRuntime(self.project_root, vibe_config)

    def run_relative_strength(self, symbols: list[str], start_date: str, end_date: str) -> dict[str, Any]:
        if not self.config.get("enabled", False):
            raise AdapterConfigurationError("Vibe backtest adapter is disabled")
        status = self.runtime.require_ready()
        run_root = self._run_root()
        run_dir = run_root / f"vbt_{uuid.uuid4().hex}"
        code_dir = run_dir / "code"
        code_dir.mkdir(parents=True, exist_ok=False)
        template = self.project_root / "assets" / "templates" / "vibe_relative_strength_signal.py"
        shutil.copyfile(template, code_dir / "signal_engine.py")
        config = {
            "codes": [VibeMarketDataAdapter.project_symbol(symbol) for symbol in symbols],
            "start_date": start_date,
            "end_date": end_date,
            "source": str(self.config.get("source", "yahoo")),
            "interval": str(self.config.get("interval", "1D")),
            "engine": "daily",
            "initial_cash": 2000,
            "slippage_bps": 5,
        }
        (run_dir / "config.json").write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        result = self.runtime.bridge("run_backtest", {"run_dir": str(run_dir), "allowed_root": str(run_root)})
        metrics_path = Path(str(result["metrics_csv"]))
        if not metrics_path.is_file():
            raise AdapterDataError("Vibe backtest did not produce metrics.csv")
        return {"status": "completed", "run_dir": str(run_dir), "metrics_csv": str(metrics_path), "vibe_commit": status.actual_commit}

    def _run_root(self) -> Path:
        configured = Path(str(self.config.get("run_root", "state/vibe_backtests")))
        resolved = configured.resolve() if configured.is_absolute() else (self.project_root / configured).resolve()
        try:
            resolved.relative_to(self.project_root)
        except ValueError as exc:
            raise AdapterConfigurationError("Vibe backtest run root must stay inside the project") from exc
        resolved.mkdir(parents=True, exist_ok=True)
        return resolved
