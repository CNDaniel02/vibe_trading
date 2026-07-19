from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from apscheduler.schedulers.background import BackgroundScheduler

from scripts.runtime.heartbeat import write_heartbeat
from scripts.runtime.process_lock import ProcessLock


class PaperScheduler:
    def __init__(self, root: str | Path, timezone: str = "America/New_York") -> None:
        self.root = Path(root)
        self.scheduler = BackgroundScheduler(timezone=timezone)

    def add_interval_job(self, job_id: str, seconds: int, func: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        def guarded_job() -> None:
            lock = ProcessLock(self.root / "state" / f"{job_id}.lock")
            if not lock.acquire():
                write_heartbeat(self.root, "running", {"job_id": job_id, "skipped": "lock_held"})
                return
            try:
                write_heartbeat(self.root, "running", {"job_id": job_id})
                func(*args, **kwargs)
                write_heartbeat(self.root, "ok", {"job_id": job_id})
            except Exception as exc:
                write_heartbeat(self.root, "failed", {"job_id": job_id, "error": str(exc)})
                raise
            finally:
                lock.release()

        self.scheduler.add_job(guarded_job, "interval", seconds=seconds, id=job_id, max_instances=1, coalesce=True, replace_existing=True)

    def start(self) -> None:
        self.scheduler.start()

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
