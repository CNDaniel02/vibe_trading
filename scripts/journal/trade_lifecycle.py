from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

from scripts.core.audit import append_jsonl
from scripts.core.models import Fill, Quote, parse_ts
from scripts.core.state import JsonStateStore
from scripts.options.models import OptionFill, OptionQuote


class TradeLifecycleJournal:
    """Durable entry-to-exit linkage with deterministic post-trade statistics."""

    FILENAME = "trade_lifecycle.json"

    def __init__(self, root: str | Path, namespace: str | None = None) -> None:
        self.root = Path(root)
        self.namespace = namespace
        self.store = JsonStateStore(root, namespace=namespace)
        self.log_prefix = f"strategy_sleeves/{namespace}/" if namespace else ""

    def record_equity_fill(self, fill: Fill, thesis: str) -> dict[str, Any]:
        state = self._state()
        key = f"equity:{fill.symbol}"
        if fill.side == "buy":
            trade = {
                "trade_id": f"trade_{uuid4().hex}",
                "instrument": "equity",
                "symbol": fill.symbol,
                "entry_order_id": fill.order_id,
                "entry_time": fill.filled_at,
                "entry_price": fill.price,
                "quantity": fill.quantity,
                "entry_commission": fill.commission,
                "entry_slippage_per_unit": fill.slippage_usd_per_share,
                "thesis": thesis,
                "mfe_pct": 0.0,
                "mae_pct": 0.0,
                "status": "open",
            }
            state["open"][key] = trade
            event = {"event": "trade_opened", **trade}
        else:
            trade = state["open"].pop(key, None)
            if trade is None:
                event = {"event": "trade_close_unmatched", "instrument": "equity", "symbol": fill.symbol}
            else:
                event = self._close_trade(
                    trade,
                    exit_order_id=fill.order_id,
                    exit_time=fill.filled_at,
                    exit_price=fill.price,
                    exit_commission=fill.commission,
                    exit_slippage=fill.slippage_usd_per_share,
                    multiplier=1,
                )
                state["closed"].append(event)
                self._write_postmortem(event)
        self.store.write_json(self.FILENAME, state)
        append_jsonl(self.root, f"{self.log_prefix}trade_journal.jsonl", event)
        return event

    def record_option_fill(self, fill: OptionFill, thesis: str) -> dict[str, Any]:
        state = self._state()
        key = f"option:{fill.option_id}"
        if fill.intent == "buy_to_open":
            trade = {
                "trade_id": f"trade_{uuid4().hex}",
                "instrument": fill.option_type,
                "symbol": fill.underlying,
                "option_id": fill.option_id,
                "entry_order_id": fill.order_id,
                "entry_time": fill.filled_at,
                "entry_price": fill.price,
                "quantity": fill.quantity,
                "multiplier": fill.multiplier,
                "entry_commission": fill.commission,
                "entry_slippage_per_unit": fill.slippage_usd_per_contract,
                "thesis": thesis,
                "mfe_pct": 0.0,
                "mae_pct": 0.0,
                "status": "open",
            }
            state["open"][key] = trade
            event = {"event": "trade_opened", **trade}
        else:
            trade = state["open"].pop(key, None)
            if trade is None:
                event = {"event": "trade_close_unmatched", "instrument": fill.option_type, "option_id": fill.option_id}
            else:
                event = self._close_trade(
                    trade,
                    exit_order_id=fill.order_id,
                    exit_time=fill.filled_at,
                    exit_price=fill.price,
                    exit_commission=fill.commission,
                    exit_slippage=fill.slippage_usd_per_contract,
                    multiplier=fill.multiplier,
                )
                state["closed"].append(event)
                self._write_postmortem(event)
        self.store.write_json(self.FILENAME, state)
        append_jsonl(self.root, f"{self.log_prefix}trade_journal.jsonl", event)
        return event

    def mark_equity_quotes(self, quotes: dict[str, Quote], asof: str) -> None:
        state = self._state()
        changed = False
        for key, trade in state["open"].items():
            if trade.get("instrument") != "equity":
                continue
            quote = quotes.get(str(trade["symbol"]))
            if quote is None or parse_ts(quote.asof) > parse_ts(asof):
                continue
            self._mark(trade, quote.bid, quote.asof)
            changed = True
        if changed:
            self.store.write_json(self.FILENAME, state)

    def mark_option_quotes(self, quotes: dict[str, OptionQuote], asof: str) -> None:
        state = self._state()
        changed = False
        for trade in state["open"].values():
            option_id = trade.get("option_id")
            if not option_id:
                continue
            quote = quotes.get(str(option_id))
            if quote is None or parse_ts(quote.updated_at) > parse_ts(asof):
                continue
            self._mark(trade, quote.bid, quote.updated_at)
            changed = True
        if changed:
            self.store.write_json(self.FILENAME, state)

    @staticmethod
    def _mark(trade: dict[str, Any], liquidation_price: float, quote_time: str) -> None:
        entry = float(trade["entry_price"])
        return_pct = (liquidation_price / entry - 1) * 100 if entry > 0 else 0.0
        trade["mfe_pct"] = round(max(float(trade.get("mfe_pct", 0)), return_pct), 6)
        trade["mae_pct"] = round(min(float(trade.get("mae_pct", 0)), return_pct), 6)
        trade["last_mark_at"] = quote_time
        trade["last_mark_price"] = liquidation_price

    @staticmethod
    def _close_trade(
        trade: dict[str, Any],
        *,
        exit_order_id: str,
        exit_time: str,
        exit_price: float,
        exit_commission: float,
        exit_slippage: float,
        multiplier: int,
    ) -> dict[str, Any]:
        quantity = float(trade["quantity"])
        entry_price = float(trade["entry_price"])
        pnl = (exit_price - entry_price) * quantity * multiplier
        pnl -= float(trade.get("entry_commission", 0)) + exit_commission
        return_pct = (exit_price / entry_price - 1) * 100 if entry_price > 0 else 0.0
        holding_minutes = (
            parse_ts(exit_time) - parse_ts(str(trade["entry_time"]))
        ).total_seconds() / 60
        return {
            **trade,
            "event": "trade_closed",
            "status": "closed",
            "exit_order_id": exit_order_id,
            "exit_time": exit_time,
            "exit_price": exit_price,
            "exit_commission": exit_commission,
            "exit_slippage_per_unit": exit_slippage,
            "realized_pnl": round(pnl, 4),
            "return_pct": round(return_pct, 6),
            "holding_minutes": round(holding_minutes, 2),
            "outcome": "win" if pnl > 0 else ("loss" if pnl < 0 else "flat"),
        }

    def _write_postmortem(self, trade: dict[str, Any]) -> None:
        journal_dir = self.root / "logs" / "journal"
        if self.namespace:
            journal_dir = self.root / "logs" / "strategy_sleeves" / self.namespace / "journal"
        journal_dir.mkdir(parents=True, exist_ok=True)
        path = journal_dir / f"{trade['trade_id']}.md"
        lines = [
            f"# Paper Trade Journal: {trade['trade_id']}",
            "",
            f"- Instrument: {trade['instrument']}",
            f"- Symbol: {trade['symbol']}",
            f"- Entry: {trade['entry_time']} @ {trade['entry_price']}",
            f"- Exit: {trade['exit_time']} @ {trade['exit_price']}",
            f"- Quantity: {trade['quantity']}",
            f"- Realized PnL: ${trade['realized_pnl']:.2f}",
            f"- Return: {trade['return_pct']:.4f}%",
            f"- MFE: {trade.get('mfe_pct', 0):.4f}%",
            f"- MAE: {trade.get('mae_pct', 0):.4f}%",
            f"- Holding minutes: {trade['holding_minutes']}",
            f"- Thesis: {trade.get('thesis', '')}",
            "",
            "## Postmortem",
            f"Outcome: {trade['outcome']}. The record is based on observed bid/ask marks and adverse simulated execution.",
            "",
            "## Next Evaluation",
            "Compare the original score and evidence against the realized return; do not change policy from one trade.",
            "",
        ]
        content = "\n".join(lines)
        path.write_text(content, encoding="utf-8")
        entry_order_id = trade.get("entry_order_id")
        if entry_order_id:
            (journal_dir / f"{entry_order_id}.md").write_text(content, encoding="utf-8")

    def _state(self) -> dict[str, Any]:
        state = self.store.read_json(self.FILENAME, {"open": {}, "closed": []})
        state.setdefault("open", {})
        state.setdefault("closed", [])
        return state
