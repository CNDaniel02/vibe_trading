from __future__ import annotations

from pathlib import Path

from scripts.core.models import Order


def write_order_journal(root: str | Path, order: Order, note: str = "") -> Path:
    journal_dir = Path(root) / "logs" / "journal"
    journal_dir.mkdir(parents=True, exist_ok=True)
    path = journal_dir / f"{order.order_id}.md"
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
        "## Postmortem",
        "Pending until the trade is closed.",
        "",
    ]
    path.write_text("\n".join(body), encoding="utf-8")
    return path
