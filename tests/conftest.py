from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture
def paper_root(tmp_path: Path) -> Path:
    root = tmp_path / "paper"
    root.mkdir()
    shutil.copytree(PROJECT_ROOT / "config", root / "config")
    # Tests must never inherit a developer's configured LLM provider or spend
    # credentials from the project-level .env.local file.
    llm_path = root / "config" / "llm.yaml"
    llm_config = yaml.safe_load(llm_path.read_text(encoding="utf-8"))
    llm_config["provider"] = "mock"
    llm_path.write_text(yaml.safe_dump(llm_config, sort_keys=False), encoding="utf-8")
    paper_mode_path = root / "config" / "paper_mode.yaml"
    paper_mode = yaml.safe_load(paper_mode_path.read_text(encoding="utf-8"))
    paper_mode.setdefault("strategy_lines", {})["options"] = False
    paper_mode_path.write_text(yaml.safe_dump(paper_mode, sort_keys=False), encoding="utf-8")
    (root / "state").mkdir()
    (root / "logs").mkdir()
    (root / "state" / "paper_account.json").write_text(
        json.dumps({"cash": 2000, "initial_cash": 2000, "realized_pnl": 0, "updated_at": "2026-07-04T14:00:00+00:00"}),
        encoding="utf-8",
    )
    (root / "state" / "paper_positions.json").write_text("{}", encoding="utf-8")
    (root / "state" / "paper_orders.json").write_text("{}", encoding="utf-8")
    (root / "state" / "paper_option_positions.json").write_text("{}", encoding="utf-8")
    (root / "state" / "paper_option_orders.json").write_text("{}", encoding="utf-8")
    (root / "state" / "daily_counters.json").write_text(
        json.dumps({"date": "2026-07-04", "trades": 0}),
        encoding="utf-8",
    )
    return root
