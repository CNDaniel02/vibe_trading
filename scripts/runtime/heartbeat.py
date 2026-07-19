from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from scripts.core.models import parse_ts, utc_now


def heartbeat_path(root: str | Path) -> Path:
    path = Path(root) / "state" / "runtime_heartbeat.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def write_heartbeat(root: str | Path, status: str = "ok", payload: dict[str, Any] | None = None, now: str | None = None) -> dict[str, Any]:
    record = {
        "last_heartbeat_at": now or utc_now(),
        "status": status,
        "payload": payload or {},
    }
    heartbeat_path(root).write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return record


def read_heartbeat(root: str | Path) -> dict[str, Any] | None:
    path = heartbeat_path(root)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def heartbeat_age_seconds(root: str | Path, now: str | None = None) -> float | None:
    record = read_heartbeat(root)
    if record is None:
        return None
    current = parse_ts(now or utc_now())
    last = parse_ts(record["last_heartbeat_at"])
    return (current - last).total_seconds()
