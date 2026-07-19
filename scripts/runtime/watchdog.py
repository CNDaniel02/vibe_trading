from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

from scripts.runtime.heartbeat import heartbeat_age_seconds, read_heartbeat


@dataclass(frozen=True)
class WatchdogDecision:
    healthy: bool
    fail_closed: bool
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


def check_runtime(root: str | Path, max_heartbeat_age_seconds: int = 120, now: str | None = None) -> WatchdogDecision:
    record = read_heartbeat(root)
    if record is None:
        return WatchdogDecision(False, True, "missing heartbeat")
    age = heartbeat_age_seconds(root, now)
    if age is None:
        return WatchdogDecision(False, True, "missing heartbeat age")
    if age < -1:
        return WatchdogDecision(False, True, "future heartbeat")
    if age > max_heartbeat_age_seconds:
        return WatchdogDecision(False, True, "stale heartbeat")
    if record.get("status") not in ("ok", "idle", "running"):
        return WatchdogDecision(False, True, f"bad heartbeat status: {record.get('status')}")
    return WatchdogDecision(True, False, "runtime healthy")
