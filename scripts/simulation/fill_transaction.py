from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from scripts.core.file_lock import InterProcessFileLock
from scripts.core.models import utc_now
from scripts.core.state import JsonStateStore


class PaperFillTransactionCoordinator:
    """Crash-safe WAL for the JSON files changed by one simulated fill."""

    FILENAME = "paper_fill_transactions.json"

    def __init__(self, store: JsonStateStore) -> None:
        self.store = store
        self.root = store.root
        self.lock_path = store.path("paper_fill_commit.lock")

    def lock(self) -> InterProcessFileLock:
        return InterProcessFileLock(self.lock_path)

    def recover(self) -> None:
        with self.lock():
            self.recover_locked()

    def recover_locked(self) -> None:
        ledger = self._ledger()
        changed = False
        for transaction_id, record in ledger["transactions"].items():
            if record.get("status") != "prepared":
                continue
            self._apply(record)
            self._mark_committed(record)
            changed = True
        if changed:
            self.store.write_json(self.FILENAME, ledger)

    def commit_locked(self, transaction: dict[str, Any]) -> None:
        transaction_id = str(transaction["transaction_id"])
        ledger = self._ledger()
        existing = ledger["transactions"].get(transaction_id)
        if existing is not None and existing.get("status") == "committed":
            return
        if existing is None:
            record = {**transaction, "status": "prepared"}
            ledger["transactions"][transaction_id] = record
            self.store.write_json(self.FILENAME, ledger)
        else:
            record = existing

        self._apply(record)
        self._mark_committed(record)
        self.store.write_json(self.FILENAME, ledger)

    def _ledger(self) -> dict[str, Any]:
        ledger = self.store.read_json(
            self.FILENAME,
            {"version": 1, "transactions": {}},
        )
        ledger.setdefault("version", 1)
        ledger.setdefault("transactions", {})
        return ledger

    def _apply(self, record: dict[str, Any]) -> None:
        transaction_id = str(record["transaction_id"])
        for write in record.get("state_writes", []):
            self.store.write_json(str(write["name"]), write["data"])
        for write in record.get("jsonl_writes", []):
            payload = dict(write["record"])
            payload.setdefault("fill_transaction_id", transaction_id)
            self._append_jsonl_once(
                self.root / "logs" / str(write["filename"]),
                payload,
                transaction_id,
            )
        for write in record.get("text_writes", []):
            self._write_text_atomic(
                self.root / "logs" / str(write["filename"]),
                str(write["content"]),
            )

    @staticmethod
    def _mark_committed(record: dict[str, Any]) -> None:
        record["status"] = "committed"
        record["committed_at"] = utc_now(timespec="microseconds")
        record["state_files"] = [write["name"] for write in record.get("state_writes", [])]
        record["jsonl_files"] = [write["filename"] for write in record.get("jsonl_writes", [])]
        record["text_files"] = [write["filename"] for write in record.get("text_writes", [])]
        record.pop("state_writes", None)
        record.pop("jsonl_writes", None)
        record.pop("text_writes", None)

    @staticmethod
    def _append_jsonl_once(path: Path, payload: dict[str, Any], transaction_id: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.with_suffix(path.suffix + ".lock")
        with InterProcessFileLock(lock_path):
            if path.exists():
                for line in path.read_text(encoding="utf-8-sig").splitlines():
                    try:
                        existing = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if existing.get("fill_transaction_id") == transaction_id:
                        return
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(payload, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())

    @staticmethod
    def _write_text_atomic(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.with_suffix(path.suffix + ".lock")
        with InterProcessFileLock(lock_path):
            tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
            with tmp.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            tmp.replace(path)


def counters_after_fill(
    counters: dict[str, Any],
    *,
    line: str,
    is_entry: bool,
    realized_pnl_delta: float = 0.0,
) -> dict[str, Any]:
    updated = dict(counters)
    if is_entry:
        updated["trades"] = int(updated.get("trades", 0)) + 1
        line_key = "option_trades" if line == "options" else "equity_trades"
        updated[line_key] = int(updated.get(line_key, 0)) + 1
    else:
        updated["daily_realized_pnl"] = round(
            float(updated.get("daily_realized_pnl", 0)) + realized_pnl_delta,
            8,
        )
        line_key = "option_realized_pnl" if line == "options" else "equity_realized_pnl"
        updated[line_key] = round(
            float(updated.get(line_key, 0)) + realized_pnl_delta,
            8,
        )
    return updated
