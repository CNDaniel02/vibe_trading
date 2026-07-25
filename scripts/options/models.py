from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Literal

from scripts.core.models import OrderStatus, parse_ts, utc_now


OptionType = Literal["call", "put"]
OptionIntent = Literal["buy_to_open", "sell_to_close"]


@dataclass(frozen=True)
class OptionContract:
    option_id: str
    chain_id: str
    underlying: str
    option_type: OptionType
    strike_price: float
    expiration_date: str
    multiplier: int = 100
    exercise_style: str = "american"
    settlement_type: str = "physical"
    sellout_datetime: str | None = None
    below_tick: float = 0.01
    above_tick: float = 0.05
    tick_cutoff_price: float = 3.0

    def dte(self, now: str) -> int:
        return (date.fromisoformat(self.expiration_date) - parse_ts(now).date()).days

    def key(self) -> str:
        return self.option_id

    def display_symbol(self) -> str:
        return f"{self.underlying} {self.expiration_date} {self.strike_price:g}{self.option_type[0].upper()}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OptionContract":
        return cls(**data)


@dataclass(frozen=True)
class OptionQuote:
    option_id: str
    bid: float
    ask: float
    mark: float
    updated_at: str
    source: str
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    rho: float | None = None
    implied_volatility: float | None = None
    volume: int | None = None
    open_interest: int | None = None
    bid_size: int | None = None
    ask_size: int | None = None
    chance_of_profit_long: float | None = None

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    def spread_pct(self) -> float:
        return (self.ask - self.bid) / self.mid if self.mid > 0 else float("inf")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OptionQuote":
        return cls(**data)


@dataclass
class OptionOrder:
    order_id: str
    decision_id: str
    contract: OptionContract
    intent: OptionIntent
    quantity: int
    order_type: Literal["market", "limit"]
    limit_price: float | None
    status: OrderStatus = "created"
    filled_quantity: int = 0
    average_fill_price: float | None = None
    created_at: str = field(default_factory=utc_now)
    submitted_at: str | None = None
    updated_at: str | None = None
    quote_seen_at: str | None = None
    reject_reason: str | None = None
    idempotency_key: str | None = None
    thesis: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["contract"] = self.contract.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OptionOrder":
        value = dict(data)
        value["contract"] = OptionContract.from_dict(value["contract"])
        return cls(**value)


@dataclass(frozen=True)
class OptionFill:
    fill_id: str
    order_id: str
    option_id: str
    underlying: str
    option_type: OptionType
    intent: OptionIntent
    quantity: int
    price: float
    multiplier: int
    gross_amount: float
    commission: float
    slippage_usd_per_contract: float
    quote_asof: str
    filled_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class OptionPosition:
    contract: OptionContract
    quantity: int
    average_price: float
    opened_at: str
    updated_at: str
    realized_pnl: float = 0.0

    def cost_basis(self) -> float:
        return self.quantity * self.average_price * self.contract.multiplier

    def liquidation_value(self, quote: OptionQuote | None) -> float:
        return self.quantity * (quote.bid if quote else self.average_price) * self.contract.multiplier

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["contract"] = self.contract.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OptionPosition":
        value = dict(data)
        value["contract"] = OptionContract.from_dict(value["contract"])
        return cls(**value)
