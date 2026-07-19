from __future__ import annotations

from dataclasses import dataclass

from scripts.core.models import parse_ts


@dataclass
class VirtualClock:
    current_time: str | None = None

    def advance_to(self, timestamp: str) -> str:
        parsed = parse_ts(timestamp)
        if self.current_time is not None and parsed < parse_ts(self.current_time):
            raise ValueError("virtual clock cannot move backwards")
        self.current_time = parsed.isoformat(timespec="seconds")
        return self.current_time

    def now(self) -> str:
        if self.current_time is None:
            raise RuntimeError("virtual clock has not started")
        return self.current_time
