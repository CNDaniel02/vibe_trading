from __future__ import annotations

from pathlib import Path

from scripts.core.state import JsonStateStore
from scripts.options.models import OptionOrder, OptionPosition


class OptionStateStore:
    """Options state with the cash account intentionally shared with equities."""

    def __init__(
        self,
        root: str | Path,
        initial_cash: float = 2000.0,
        namespace: str | None = None,
    ) -> None:
        self.base = JsonStateStore(root, initial_cash, namespace=namespace)
        self.base.ensure()

    def positions(self) -> dict[str, OptionPosition]:
        raw = self.base.read_json("paper_option_positions.json", {})
        return {option_id: OptionPosition.from_dict(value) for option_id, value in raw.items()}

    def save_positions(self, positions: dict[str, OptionPosition]) -> None:
        self.base.write_json("paper_option_positions.json", {key: value.to_dict() for key, value in positions.items()})

    def orders(self) -> dict[str, OptionOrder]:
        raw = self.base.read_json("paper_option_orders.json", {})
        return {order_id: OptionOrder.from_dict(value) for order_id, value in raw.items()}

    def save_orders(self, orders: dict[str, OptionOrder]) -> None:
        self.base.write_json("paper_option_orders.json", {key: value.to_dict() for key, value in orders.items()})
