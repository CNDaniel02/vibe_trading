from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd

from scripts.core.models import parse_ts, utc_now


@dataclass(frozen=True)
class MarketClockState:
    asof: str
    session: str
    market_session: str
    is_regular: bool
    open_time: str | None
    close_time: str | None
    minutes_to_close: float | None
    session_progress: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class UsEquityMarketClock:
    def __init__(self) -> None:
        self.calendar = xcals.get_calendar("XNYS")
        self.new_york = ZoneInfo("America/New_York")

    def status(self, now: str | None = None) -> MarketClockState:
        parsed = parse_ts(now or utc_now())
        minute = pd.Timestamp(parsed).floor("min")
        local = parsed.astimezone(self.new_york)
        day = pd.Timestamp(local.date())
        if not self.calendar.is_session(day):
            return MarketClockState(parsed.isoformat(), day.date().isoformat(), "closed", False, None, None, None, 0.0)

        session = self.calendar.date_to_session(day, direction="none")
        open_time = self.calendar.session_open(session)
        close_time = self.calendar.session_close(session)
        if minute < open_time:
            market_session = "pre_market"
        elif minute >= close_time:
            market_session = "after_hours"
        else:
            market_session = "regular"
        regular = market_session == "regular" and self.calendar.is_open_on_minute(minute, ignore_breaks=True)
        minutes_to_close = max(0.0, (close_time - minute).total_seconds() / 60) if regular else None
        total = max(1.0, (close_time - open_time).total_seconds())
        progress = max(0.0, min(1.0, (minute - open_time).total_seconds() / total)) if regular else 0.0
        return MarketClockState(
            parsed.isoformat(),
            session.date().isoformat(),
            market_session,
            regular,
            open_time.isoformat(),
            close_time.isoformat(),
            minutes_to_close,
            progress,
        )
