from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import timedelta
from pathlib import Path
from typing import Any

from scripts.core.models import parse_ts, utc_now


class EvidenceSnapshotStore:
    """Immutable evidence snapshots plus small mutable cooldown indexes."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.snapshot_dir = self.root / "logs" / "catalyst_snapshots"
        self.cooldown_path = self.root / "state" / "catalyst_cooldowns.json"

    def normalize_events(self, events: list[dict[str, Any]], ticker: str | None = None) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        seen_fingerprints: set[str] = set()
        seen_content: set[str] = set()
        for raw in events:
            if not isinstance(raw, dict):
                continue
            event = dict(raw)
            event_ticker = str(event.get("ticker") or ticker or "").upper() or None
            event["ticker"] = event_ticker
            headline = str(event.get("headline", "")).strip()
            published_at = str(event.get("published_at", ""))
            if not headline or not published_at:
                continue
            canonical_url = str(event.get("url", "")).strip()
            highlights = event.get("highlights", [])
            if isinstance(highlights, list):
                highlight_text = "\n".join(str(item) for item in highlights)
            else:
                highlight_text = str(highlights)
            content_hash = self._hash(f"{canonical_url}\n{headline}\n{highlight_text}")
            event_day = str(event.get("event_at") or published_at)[:10]
            fingerprint = self._hash(
                f"{event_ticker or 'MARKET'}\n{self._normalize_text(headline)}\n{event_day}"
            )
            event["canonical_url"] = canonical_url
            event["content_hash"] = content_hash
            event["event_fingerprint"] = fingerprint
            if (
                (canonical_url and canonical_url in seen_urls)
                or fingerprint in seen_fingerprints
                or content_hash in seen_content
            ):
                continue
            if canonical_url:
                seen_urls.add(canonical_url)
            seen_fingerprints.add(fingerprint)
            seen_content.add(content_hash)
            normalized.append(event)
        return normalized

    def write_snapshot(
        self,
        *,
        snapshot_type: str,
        decision_time: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        envelope = {
            "snapshot_type": snapshot_type,
            "decision_time": decision_time,
            "retrieved_at": utc_now(),
            "payload": payload,
        }
        serialized = json.dumps(envelope, separators=(",", ":"), sort_keys=True)
        snapshot_hash = self._hash(serialized)
        envelope["snapshot_hash"] = snapshot_hash
        stamp = parse_ts(decision_time).strftime("%Y%m%dT%H%M%S%fZ")
        filename = f"{stamp}-{snapshot_type}-{snapshot_hash[:12]}.json"
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        path = self.snapshot_dir / filename
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        descriptor = os.open(path, flags)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(envelope, handle, indent=2, sort_keys=True)
                handle.write("\n")
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return {"path": str(path), "snapshot_hash": snapshot_hash}

    def research_eligibility(
        self,
        ticker: str,
        events: list[dict[str, Any]],
        decision_time: str,
        *,
        ticker_cooldown_minutes: int,
        event_cooldown_hours: int,
    ) -> tuple[bool, list[dict[str, Any]], str]:
        state = self._read_cooldowns()
        ticker_state = state.get("tickers", {}).get(ticker, {})
        now = parse_ts(decision_time)
        researched = ticker_state.get("researched_events", {})
        eligible_events: list[dict[str, Any]] = []
        for event in events:
            fingerprint = str(event.get("event_fingerprint", ""))
            if not fingerprint:
                continue
            last_sent = researched.get(fingerprint)
            if last_sent and now - parse_ts(str(last_sent)) < timedelta(hours=event_cooldown_hours):
                continue
            eligible_events.append(event)
        if not events:
            return False, [], "no grounded event evidence"
        if eligible_events:
            return True, eligible_events, "new or event-cooldown-expired evidence"
        last_research = ticker_state.get("last_deep_research_at")
        if last_research and now - parse_ts(str(last_research)) < timedelta(minutes=ticker_cooldown_minutes):
            return False, [], "ticker cooldown active and no new event"
        return False, [], "all event fingerprints remain inside event cooldown"

    def mark_researched(self, ticker: str, events: list[dict[str, Any]], decision_time: str) -> None:
        state = self._read_cooldowns()
        tickers = state.setdefault("tickers", {})
        ticker_state = tickers.setdefault(ticker, {"researched_events": {}})
        ticker_state["last_deep_research_at"] = decision_time
        researched = ticker_state.setdefault("researched_events", {})
        for event in events:
            fingerprint = str(event.get("event_fingerprint", ""))
            if fingerprint:
                researched[fingerprint] = decision_time
        state["updated_at"] = utc_now()
        self._write_cooldowns(state)

    def unsent_model_events(
        self,
        scope: str,
        events: list[dict[str, Any]],
        decision_time: str,
        *,
        event_cooldown_hours: int,
    ) -> list[dict[str, Any]]:
        state = self._read_cooldowns()
        sent = state.get("model_event_scopes", {}).get(scope, {})
        now = parse_ts(decision_time)
        result: list[dict[str, Any]] = []
        for event in events:
            fingerprint = str(event.get("event_fingerprint", ""))
            if not fingerprint:
                continue
            last_sent = sent.get(fingerprint)
            if last_sent and now - parse_ts(str(last_sent)) < timedelta(hours=event_cooldown_hours):
                continue
            result.append(event)
        return result

    def mark_model_events_sent(
        self,
        scope: str,
        events: list[dict[str, Any]],
        decision_time: str,
    ) -> None:
        state = self._read_cooldowns()
        scopes = state.setdefault("model_event_scopes", {})
        sent = scopes.setdefault(scope, {})
        for event in events:
            fingerprint = str(event.get("event_fingerprint", ""))
            if fingerprint:
                sent[fingerprint] = decision_time
        state["updated_at"] = utc_now()
        self._write_cooldowns(state)

    def _read_cooldowns(self) -> dict[str, Any]:
        if not self.cooldown_path.exists():
            return {"tickers": {}}
        try:
            value = json.loads(self.cooldown_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"tickers": {}}
        return value if isinstance(value, dict) else {"tickers": {}}

    def _write_cooldowns(self, value: dict[str, Any]) -> None:
        self.cooldown_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cooldown_path.with_name(f".{self.cooldown_path.name}.tmp")
        temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(self.cooldown_path)

    @staticmethod
    def _hash(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _normalize_text(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
