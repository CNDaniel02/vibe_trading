from __future__ import annotations

import os
import time
from pathlib import Path

from scripts.runtime.process_lock import ProcessLock


class InterProcessFileLock:
    """Small cross-process lock for append-only logs and JSON state files."""

    def __init__(
        self,
        path: str | Path,
        *,
        timeout_seconds: float = 10.0,
        poll_seconds: float = 0.01,
    ) -> None:
        self.path = Path(path)
        self.timeout_seconds = timeout_seconds
        self.poll_seconds = poll_seconds
        self._lock: ProcessLock | None = None

    def acquire(self) -> bool:
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            lock = ProcessLock(self.path)
            if lock.acquire():
                self._lock = lock
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(self.poll_seconds)

    def release(self) -> None:
        if self._lock is not None:
            self._lock.release()
            self._lock = None

    def __enter__(self) -> "InterProcessFileLock":
        if not self.acquire():
            raise TimeoutError(f"timed out waiting for file lock: {self.path}")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


def durable_append(path: str | Path, line: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.with_suffix(target.suffix + ".lock")
    with InterProcessFileLock(lock_path):
        with target.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
