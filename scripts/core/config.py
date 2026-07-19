from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def load_yaml(path: str | Path) -> dict[str, Any]:
    full = Path(path)
    if not full.is_absolute():
        full = PROJECT_ROOT / full
    with full.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{full} must contain a mapping")
    return data


def load_runtime_config(root: str | Path | None = None) -> dict[str, Any]:
    base = Path(root) if root else PROJECT_ROOT
    load_dotenv(base / ".env.local", override=False)
    paper = load_yaml(base / "config" / "paper_mode.yaml")
    universe = load_yaml(base / "config" / "equity_universe.yaml")
    risk = load_yaml(base / "config" / "paper_risk_limits.yaml")
    costs = load_yaml(base / "config" / "execution_costs.yaml")
    llm_path = base / "config" / "llm.yaml"
    strategies_path = base / "config" / "strategy_profiles.yaml"
    integrations_path = base / "config" / "integrations.yaml"
    evaluation_path = base / "config" / "evaluation.yaml"
    return {
        "paper": paper,
        "universe": universe,
        "risk": risk,
        "costs": costs,
        "llm": load_yaml(llm_path) if llm_path.exists() else {"provider": "mock"},
        "strategies": load_yaml(strategies_path) if strategies_path.exists() else {},
        "integrations": load_yaml(integrations_path) if integrations_path.exists() else {},
        "evaluation": load_yaml(evaluation_path) if evaluation_path.exists() else {},
        "root": str(base),
    }


def assert_paper_mode(config: dict[str, Any]) -> None:
    mode = config["paper"].get("mode", {})
    if not mode.get("paper", False):
        raise RuntimeError("paper mode is not enabled")
    if mode.get("live_trading", False):
        raise RuntimeError("live_trading must be false for this implementation")
