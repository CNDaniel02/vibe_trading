from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


class LiveOrderToolBlocked(RuntimeError):
    pass


@dataclass
class RobinhoodReadonlyAdapter:
    get_accounts_fn: Callable[[], Any] | None = None
    get_portfolio_fn: Callable[[str], Any] | None = None
    get_equity_positions_fn: Callable[[str], Any] | None = None
    get_equity_orders_fn: Callable[[str], Any] | None = None

    def get_accounts(self) -> Any:
        if self.get_accounts_fn is None:
            raise RuntimeError("get_accounts read function is not configured")
        return self.get_accounts_fn()

    def get_portfolio(self, account_number: str) -> Any:
        if self.get_portfolio_fn is None:
            raise RuntimeError("get_portfolio read function is not configured")
        return self.get_portfolio_fn(account_number)

    def get_equity_positions(self, account_number: str) -> Any:
        if self.get_equity_positions_fn is None:
            raise RuntimeError("get_equity_positions read function is not configured")
        return self.get_equity_positions_fn(account_number)

    def get_equity_orders(self, account_number: str) -> Any:
        if self.get_equity_orders_fn is None:
            raise RuntimeError("get_equity_orders read function is not configured")
        return self.get_equity_orders_fn(account_number)

    def review_equity_order(self, *args: Any, **kwargs: Any) -> None:
        raise LiveOrderToolBlocked("live order review is disabled in paper mode")

    def place_equity_order(self, *args: Any, **kwargs: Any) -> None:
        raise LiveOrderToolBlocked("live order placement is disabled in paper mode")

    def place_option_order(self, *args: Any, **kwargs: Any) -> None:
        raise LiveOrderToolBlocked("live option placement is disabled in paper mode")
