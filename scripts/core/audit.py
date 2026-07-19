from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from scripts.core.models import utc_now


class AuditLog:
    def __init__(self, root: str | Path, filename: str = "audit.jsonl") -> None:
        self.root = Path(root)
        self.log_dir = self.root / "logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.log_dir / filename

    def append(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        record = {
            "audit_id": f"pa_{uuid.uuid4().hex}",
            "ts": utc_now(),
            "event_type": event_type,
            "payload": payload,
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
        return record


def append_jsonl(root: str | Path, filename: str, payload: dict[str, Any]) -> None:
    log_dir = Path(root) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    record = {"ts": utc_now(), **payload}
    with (log_dir / filename).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
