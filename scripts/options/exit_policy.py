from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import timedelta

from scripts.core.models import parse_ts
from scripts.options.models import OptionPosition, OptionQuote


@dataclass(frozen=True)
class OptionExitDecision:
    should_exit: bool
    reason: str
    return_pct: float | None

    def to_dict(self) -> dict:
        return asdict(self)


def evaluate_option_exit(position: OptionPosition, quote: OptionQuote | None, now: str, risk: dict) -> OptionExitDecision:
    if quote is None:
        return OptionExitDecision(False, "missing option quote; fail closed without synthetic exit", None)
    current_return = (quote.bid / position.average_price - 1) if position.average_price > 0 else None
    contract = position.contract
    dte = contract.dte(now)
    if contract.sellout_datetime:
        threshold = parse_ts(contract.sellout_datetime) - timedelta(minutes=int(risk.get("exit_before_sellout_minutes", 30)))
        if parse_ts(now) >= threshold:
            return OptionExitDecision(True, "mandatory exit before broker sellout", current_return)
    if dte <= int(risk.get("force_exit_dte", 2)):
        return OptionExitDecision(True, "mandatory exit before expiration; exercise disabled", current_return)
    held_days = (parse_ts(now).date() - parse_ts(position.opened_at).date()).days
    if held_days >= int(risk.get("max_holding_calendar_days", 5)):
        return OptionExitDecision(True, "maximum option holding period reached", current_return)
    if current_return is not None and current_return <= -float(risk.get("stop_loss_pct_of_premium", 0.35)):
        return OptionExitDecision(True, "option premium stop loss reached", current_return)
    if current_return is not None and current_return >= float(risk.get("take_profit_pct_of_premium", 0.50)):
        return OptionExitDecision(True, "option premium take profit reached", current_return)
    return OptionExitDecision(False, "hold", current_return)
