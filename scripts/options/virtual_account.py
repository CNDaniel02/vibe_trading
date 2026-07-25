from __future__ import annotations

from scripts.core.models import Account
from scripts.options.models import OptionFill, OptionPosition


def apply_option_fill(account: Account, positions: dict[str, OptionPosition], fill: OptionFill, contract) -> None:
    existing = positions.get(fill.option_id)
    if fill.intent == "buy_to_open":
        if existing is not None:
            raise ValueError("adding to an existing option position is blocked")
        total_cost = fill.gross_amount + fill.commission
        if account.cash + 1e-9 < total_cost:
            raise ValueError("insufficient shared paper cash")
        account.cash -= total_cost
        positions[fill.option_id] = OptionPosition(
            contract=contract,
            quantity=fill.quantity,
            average_price=fill.price,
            opened_at=fill.filled_at,
            updated_at=fill.filled_at,
        )
        return

    if fill.intent == "sell_to_close":
        if existing is None or existing.quantity < fill.quantity:
            raise ValueError("cannot close more than the paper option position")
        proceeds = fill.gross_amount - fill.commission
        pnl = proceeds - existing.average_price * fill.quantity * fill.multiplier
        account.cash += proceeds
        account.realized_pnl += pnl
        existing.realized_pnl += pnl
        existing.quantity -= fill.quantity
        existing.updated_at = fill.filled_at
        if existing.quantity == 0:
            del positions[fill.option_id]
        return

    raise ValueError(f"unsupported option fill intent {fill.intent}")
