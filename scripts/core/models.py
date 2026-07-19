from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

OrderStatus = Literal[
    "created",
    "submitted_to_paper_broker",
    "open",
    "partially_filled",
    "filled",
    "cancelled",
    "expired",
    "rejected",
]
OrderSide = Literal["buy", "sell"]
OrderType = Literal["market", "limit"]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_ts(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass
class Quote:
    symbol: str
    bid: float
    ask: float
    last: float
    asof: str
    source: str = "fixture"
    avg_daily_volume_usd: float | None = None
    asset_class: str = "us_equity"
    is_otc: bool = False
    is_leveraged_etf: bool = False
    is_inverse_etf: bool = False
    halted: bool = False
    session_volume: float | None = None
    previous_close: float | None = None

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    def spread_bps(self) -> float:
        if self.mid <= 0:
            return float("inf")
        return ((self.ask - self.bid) / self.mid) * 10000

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Order:
    order_id: str
    decision_id: str
    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: float
    limit_price: float | None
    status: OrderStatus = "created"
    filled_quantity: float = 0.0
    average_fill_price: float | None = None
    created_at: str = field(default_factory=utc_now)
    submitted_at: str | None = None
    updated_at: str | None = None
    quote_seen_at: str | None = None
    reject_reason: str | None = None
    idempotency_key: str | None = None
    thesis: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Order":
        return cls(**data)


@dataclass
class Fill:
    fill_id: str
    order_id: str
    symbol: str
    side: OrderSide
    quantity: float
    price: float
    gross_amount: float
    commission: float
    slippage_usd_per_share: float
    quote_asof: str
    filled_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Position:
    symbol: str
    quantity: float
    average_price: float
    opened_at: str
    updated_at: str
    realized_pnl: float = 0.0

    def market_value(self, quote: Quote | None = None) -> float:
        price = quote.bid if quote else self.average_price
        return self.quantity * price

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Position":
        return cls(**data)


@dataclass
class Account:
    cash: float
    initial_cash: float
    realized_pnl: float = 0.0
    updated_at: str = field(default_factory=utc_now)

    def equity(self, positions: dict[str, Position] | None = None, quotes: dict[str, Quote] | None = None) -> float:
        total = self.cash
        if positions:
            for symbol, position in positions.items():
                quote = quotes.get(symbol) if quotes else None
                total += position.market_value(quote)
        return total

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Account":
        return cls(**data)
