from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture
def paper_root(tmp_path: Path) -> Path:
    root = tmp_path / "paper"
    root.mkdir()
    shutil.copytree(PROJECT_ROOT / "config", root / "config")
    (root / "state").mkdir()
    (root / "logs").mkdir()
    (root / "state" / "paper_account.json").write_text(
        json.dumps({"cash": 2000, "initial_cash": 2000, "realized_pnl": 0, "updated_at": "2026-07-04T14:00:00+00:00"}),
        encoding="utf-8",
    )
    (root / "state" / "paper_positions.json").write_text("{}", encoding="utf-8")
    (root / "state" / "paper_orders.json").write_text("{}", encoding="utf-8")
    (root / "state" / "daily_counters.json").write_text(
        json.dumps({"date": "2026-07-04", "trades": 0}),
        encoding="utf-8",
    )
    return root
