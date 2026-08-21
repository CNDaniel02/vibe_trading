from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

from scripts.runtime.heartbeat import heartbeat_age_seconds, read_heartbeat
from scripts.runtime.process_lock import ProcessLock


@dataclass(frozen=True)
class WatchdogDecision:
    healthy: bool
    fail_closed: bool
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


def check_runtime(root: str | Path, max_heartbeat_age_seconds: int = 120, now: str | None = None) -> WatchdogDecision:
    lock = ProcessLock.inspect(Path(root) / "state" / "forward_service.lock")
    if not lock["present"]:
        return WatchdogDecision(False, True, "forward service lock missing")
    if lock["status"] == "malformed":
        return WatchdogDecision(False, True, "forward service lock malformed")
    if lock["alive"] is False:
        return WatchdogDecision(False, True, "forward service process is not running")
    if lock["alive"] is not True:
        return WatchdogDecision(False, True, "forward service process liveness is unknown")
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
