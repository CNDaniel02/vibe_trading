from __future__ import annotations

from scripts.core.models import Account, Fill, Position


def apply_fill(account: Account, positions: dict[str, Position], fill: Fill) -> None:
    symbol = fill.symbol
    if fill.side == "buy":
        total_cost = fill.gross_amount + fill.commission
        if account.cash + 1e-9 < total_cost:
            raise ValueError("insufficient paper cash")
        existing = positions.get(symbol)
        account.cash -= total_cost
        if existing is None:
            positions[symbol] = Position(
                symbol=symbol,
                quantity=fill.quantity,
                average_price=fill.price,
                opened_at=fill.filled_at,
                updated_at=fill.filled_at,
            )
        else:
            new_qty = existing.quantity + fill.quantity
            existing.average_price = ((existing.quantity * existing.average_price) + fill.gross_amount) / new_qty
            existing.quantity = new_qty
            existing.updated_at = fill.filled_at
        return

    if fill.side == "sell":
        existing = positions.get(symbol)
        if existing is None or existing.quantity + 1e-9 < fill.quantity:
            raise ValueError("cannot sell more than paper position")
        proceeds = fill.gross_amount - fill.commission
        cost_basis = existing.average_price * fill.quantity
        pnl = proceeds - cost_basis
        account.cash += proceeds
        account.realized_pnl += pnl
        existing.realized_pnl += pnl
        existing.quantity -= fill.quantity
        existing.updated_at = fill.filled_at
        if existing.quantity <= 1e-9:
            del positions[symbol]
        return

    raise ValueError(f"unsupported fill side {fill.side}")
