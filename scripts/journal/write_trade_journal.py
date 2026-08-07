from __future__ import annotations

from pathlib import Path
import json

from scripts.core.models import Order


def write_order_journal(
    root: str | Path,
    order: Order,
    note: str = "",
    *,
    namespace: str | None = None,
) -> Path:
    journal_dir = Path(root) / "logs" / "journal"
    if namespace:
        journal_dir = Path(root) / "logs" / "strategy_sleeves" / namespace / "journal"
    journal_dir.mkdir(parents=True, exist_ok=True)
    path = journal_dir / f"{order.order_id}.md"
    state_dir = Path(root) / "state"
    if namespace:
        state_dir = state_dir / "strategy_sleeves" / namespace
    lifecycle_path = state_dir / "trade_lifecycle.json"
    closed_trade = None
    if lifecycle_path.exists():
        lifecycle = json.loads(lifecycle_path.read_text(encoding="utf-8"))
        closed_trade = next(
            (
                trade
                for trade in lifecycle.get("closed", [])
                if order.order_id in {trade.get("entry_order_id"), trade.get("exit_order_id")}
            ),
            None,
        )
    body = [
        f"# Paper Trade Journal: {order.order_id}",
        "",
        f"- Symbol: {order.symbol}",
        f"- Side: {order.side}",
        f"- Status: {order.status}",
        f"- Quantity: {order.quantity}",
        f"- Average fill: {order.average_fill_price}",
        f"- Thesis: {order.thesis}",
        f"- Note: {note}",
        "",
    ]
    if closed_trade:
        body.extend(
            [
                "## Postmortem",
                f"Completed as {closed_trade['outcome']}: PnL ${closed_trade['realized_pnl']:.2f}, "
                f"return {closed_trade['return_pct']:.4f}%, MFE {closed_trade.get('mfe_pct', 0):.4f}%, "
                f"MAE {closed_trade.get('mae_pct', 0):.4f}%.",
                "",
            ]
        )
    else:
        body.extend(["## Postmortem", "Pending until the trade is closed.", ""])
    path.write_text("\n".join(body), encoding="utf-8")
    return path
