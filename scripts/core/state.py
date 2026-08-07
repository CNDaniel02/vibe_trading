from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from scripts.core.file_lock import InterProcessFileLock
from scripts.core.models import Account, Order, Position, parse_ts, utc_now


class JsonStateStore:
    def __init__(
        self,
        root: str | Path,
        initial_cash: float = 2000.0,
        namespace: str | None = None,
    ) -> None:
        self.root = Path(root)
        self.namespace = namespace
        self.state_dir = self.root / "state"
        if namespace:
            self.state_dir = self.state_dir / "strategy_sleeves" / namespace
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.initial_cash = initial_cash

    def path(self, name: str) -> Path:
        return self.state_dir / name

    def ensure(self) -> None:
        self._ensure_file("paper_account.json", Account(cash=self.initial_cash, initial_cash=self.initial_cash).to_dict())
        self._ensure_file("paper_positions.json", {})
        self._ensure_file("paper_orders.json", {})
        self._ensure_file("paper_option_positions.json", {})
        self._ensure_file("paper_option_orders.json", {})
        self._ensure_file("daily_counters.json", self._empty_counters(utc_now()[:10]))

    @staticmethod
    def _empty_counters(day: str) -> dict[str, Any]:
        return {
            "date": day,
            "trades": 0,
            "equity_trades": 0,
            "option_trades": 0,
            "daily_realized_pnl": 0.0,
            "equity_realized_pnl": 0.0,
            "option_realized_pnl": 0.0,
        }

    def _ensure_file(self, name: str, default: Any) -> None:
        path = self.path(name)
        if not path.exists():
            self.write_json(name, default)

    def read_json(self, name: str, default: Any) -> Any:
        path = self.path(name)
        if not path.exists():
            return default
        with path.open("r", encoding="utf-8-sig") as handle:
            return json.load(handle)

    def write_json(self, name: str, data: Any) -> None:
        path = self.path(name)
        lock_path = path.with_suffix(path.suffix + ".lock")
        with InterProcessFileLock(lock_path):
            tmp = path.with_name(f"{path.name}.{id(self)}.tmp")
            with tmp.open("w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
            tmp.replace(path)

    def account(self) -> Account:
        self.ensure()
        return Account.from_dict(self.read_json("paper_account.json", {}))

    def save_account(self, account: Account, asof: str | None = None) -> None:
        account.updated_at = asof or utc_now()
        self.write_json("paper_account.json", account.to_dict())

    def positions(self) -> dict[str, Position]:
        self.ensure()
        raw = self.read_json("paper_positions.json", {})
        return {symbol: Position.from_dict(value) for symbol, value in raw.items()}

    def save_positions(self, positions: dict[str, Position]) -> None:
        self.write_json("paper_positions.json", {symbol: pos.to_dict() for symbol, pos in positions.items()})

    def orders(self) -> dict[str, Order]:
        self.ensure()
        raw = self.read_json("paper_orders.json", {})
        return {order_id: Order.from_dict(value) for order_id, value in raw.items()}

    def save_orders(self, orders: dict[str, Order]) -> None:
        self.write_json("paper_orders.json", {order_id: order.to_dict() for order_id, order in orders.items()})

    def daily_counters(self, asof: str | None = None) -> dict[str, Any]:
        self.ensure()
        today = parse_ts(asof).date().isoformat() if asof else utc_now()[:10]
        counters = self.read_json("daily_counters.json", self._empty_counters(today))
        if counters.get("date") != today:
            counters = self._empty_counters(today)
            self.write_json("daily_counters.json", counters)
        for key, value in self._empty_counters(today).items():
            counters.setdefault(key, value)
        return counters

    def increment_trades(self, asof: str | None = None, line: str = "equity") -> int:
        counters = self.daily_counters(asof)
        counters["trades"] = int(counters.get("trades", 0)) + 1
        line_key = "option_trades" if line == "options" else "equity_trades"
        counters[line_key] = int(counters.get(line_key, 0)) + 1
        self.write_json("daily_counters.json", counters)
        return counters["trades"]

    def add_daily_realized_pnl(self, amount: float, asof: str | None = None, line: str = "equity") -> float:
        counters = self.daily_counters(asof)
        counters["daily_realized_pnl"] = round(float(counters.get("daily_realized_pnl", 0)) + float(amount), 8)
        line_key = "option_realized_pnl" if line == "options" else "equity_realized_pnl"
        counters[line_key] = round(float(counters.get(line_key, 0)) + float(amount), 8)
        self.write_json("daily_counters.json", counters)
        return float(counters["daily_realized_pnl"])
