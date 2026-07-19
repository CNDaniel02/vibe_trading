from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import timedelta, timezone

from scripts.core.models import Position, Quote, parse_ts


@dataclass(frozen=True)
class ExitDecision:
    should_exit: bool
    reason: str
    trigger_price: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def evaluate_position_exit(
    position: Position,
    quote: Quote | None,
    now: str,
    risk_config: dict,
    *,
    minutes_to_close: float | None = None,
    exit_before_close_minutes: int = 10,
) -> ExitDecision:
    if quote is None or quote.bid <= 0:
        return ExitDecision(False, "missing usable exit quote")
    if minutes_to_close is not None and minutes_to_close <= exit_before_close_minutes:
        return ExitDecision(True, "exit before market close", quote.bid)
    stop_price = position.average_price * (1 - float(risk_config.get("stop_loss_pct", 0.03)))
    if quote.bid <= stop_price:
        return ExitDecision(True, "deterministic stop loss", quote.bid)
    profit_price = position.average_price * (1 + float(risk_config.get("take_profit_pct", 0.06)))
    if quote.bid >= profit_price:
        return ExitDecision(True, "deterministic take profit", quote.bid)
    max_days = int(risk_config.get("max_holding_calendar_days", 5))
    if parse_ts(now) - parse_ts(position.opened_at) >= timedelta(days=max_days):
        return ExitDecision(True, "deterministic time stop", quote.bid)
    return ExitDecision(False, "position remains within exit limits")


def should_exit_before_close(now: str, close_time: str = "16:00", minutes_before_close: int = 10) -> bool:
    dt = parse_ts(now).astimezone(timezone(timedelta(hours=-4)))
    close_hour, close_minute = [int(part) for part in close_time.split(":", 1)]
    close_dt = dt.replace(hour=close_hour, minute=close_minute, second=0, microsecond=0)
    return close_dt - timedelta(minutes=minutes_before_close) <= dt < close_dt
