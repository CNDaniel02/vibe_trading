from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

from scripts.core.models import parse_ts, utc_now


EVENT_RELATION_TYPES = frozenset(
    {"new_event", "duplicate", "clarification", "material_update", "contradiction", "follow_up"}
)
EVENT_TYPES = EVENT_RELATION_TYPES
SIGNALABLE_EVENT_TYPES = frozenset(EVENT_RELATION_TYPES - {"duplicate"})


class NewsEventStore:
    """Small SQLite ledger for research and shadow decisions only.

    This class deliberately has no broker, account, order, or scheduler behavior.
    The database is an evidence ledger; inserting a proposal never creates an order.
    """

    def __init__(self, database: str | Path, *, connection: sqlite3.Connection | None = None) -> None:
        self.database = database
        self._owns_connection = connection is None
        if connection is not None:
            self.connection = connection
        else:
            if str(database) != ":memory:":
                Path(database).parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(str(database), timeout=10.0)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 10000")
        if str(database) != ":memory:":
            self.connection.execute("PRAGMA journal_mode = WAL")
            self.connection.execute("PRAGMA synchronous = FULL")
        self.initialize_schema()

    def __enter__(self) -> "NewsEventStore":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_connection:
            self.connection.close()

    def initialize_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS news_events (
                event_id TEXT PRIMARY KEY,
                ticker TEXT NOT NULL,
                headline TEXT NOT NULL,
                url TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL,
                source_tier INTEGER,
                published_at TEXT NOT NULL,
                event_at TEXT,
                first_seen_at TEXT NOT NULL,
                retrieved_at TEXT,
                content_hash TEXT NOT NULL,
                event_fingerprint TEXT NOT NULL,
                event_type TEXT NOT NULL CHECK (event_type IN (
                    'new_event', 'duplicate', 'clarification', 'material_update',
                    'contradiction', 'follow_up'
                )),
                is_duplicate INTEGER NOT NULL DEFAULT 0 CHECK (is_duplicate IN (0, 1)),
                signal_required INTEGER NOT NULL DEFAULT 1 CHECK (signal_required IN (0, 1)),
                signal_emitted INTEGER NOT NULL DEFAULT 0 CHECK (signal_emitted IN (0, 1)),
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (ticker, event_fingerprint)
            );

            CREATE TABLE IF NOT EXISTS event_relations (
                relation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_event_id TEXT NOT NULL,
                target_event_id TEXT NOT NULL,
                relation_type TEXT NOT NULL CHECK (relation_type IN (
                    'new_event', 'duplicate', 'clarification', 'material_update',
                    'contradiction', 'follow_up'
                )),
                created_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                UNIQUE (source_event_id, target_event_id, relation_type),
                FOREIGN KEY (source_event_id) REFERENCES news_events(event_id) ON DELETE CASCADE,
                FOREIGN KEY (target_event_id) REFERENCES news_events(event_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS llm_signals (
                signal_id TEXT PRIMARY KEY,
                event_id TEXT NOT NULL,
                signal_type TEXT NOT NULL DEFAULT 'news',
                direction TEXT,
                action TEXT,
                confidence REAL,
                model TEXT NOT NULL DEFAULT '',
                prompt_version TEXT NOT NULL DEFAULT '',
                signal_time TEXT NOT NULL,
                decision_time TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (event_id, signal_type, model, prompt_version, signal_time),
                FOREIGN KEY (event_id) REFERENCES news_events(event_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS tradability_observations (
                observation_id TEXT PRIMARY KEY,
                event_id TEXT,
                ticker TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                bid REAL,
                ask REAL,
                last REAL,
                spread_bps REAL,
                volume REAL,
                is_tradable INTEGER,
                rejection_reason TEXT,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (event_id, ticker, observed_at, payload_json),
                FOREIGN KEY (event_id) REFERENCES news_events(event_id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS outcome_labels (
                label_id TEXT PRIMARY KEY,
                event_id TEXT,
                signal_id TEXT,
                proposal_id TEXT,
                label TEXT NOT NULL,
                horizon TEXT NOT NULL,
                return_pct REAL,
                outcome_time TEXT NOT NULL,
                label_time TEXT NOT NULL,
                decision_time TEXT,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (signal_id, proposal_id, label, horizon, outcome_time),
                FOREIGN KEY (event_id) REFERENCES news_events(event_id) ON DELETE SET NULL,
                FOREIGN KEY (signal_id) REFERENCES llm_signals(signal_id) ON DELETE SET NULL,
                FOREIGN KEY (proposal_id) REFERENCES shadow_proposals(proposal_id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS shadow_proposals (
                proposal_id TEXT PRIMARY KEY,
                event_id TEXT,
                signal_id TEXT,
                ticker TEXT NOT NULL,
                action TEXT NOT NULL,
                direction TEXT,
                decision_time TEXT NOT NULL,
                entry_price REAL,
                stop_price REAL,
                target_price REAL,
                status TEXT NOT NULL DEFAULT 'shadow_only' CHECK (status = 'shadow_only'),
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (signal_id, ticker, action, decision_time),
                FOREIGN KEY (event_id) REFERENCES news_events(event_id) ON DELETE SET NULL,
                FOREIGN KEY (signal_id) REFERENCES llm_signals(signal_id) ON DELETE SET NULL
            );

            CREATE INDEX IF NOT EXISTS idx_news_events_ticker_time
                ON news_events (ticker, published_at);
            CREATE INDEX IF NOT EXISTS idx_news_events_pending_signal
                ON news_events (signal_required, signal_emitted, first_seen_at);
            CREATE INDEX IF NOT EXISTS idx_event_relations_target
                ON event_relations (target_event_id, relation_type);
            CREATE INDEX IF NOT EXISTS idx_llm_signals_event_time
                ON llm_signals (event_id, signal_time);
            CREATE INDEX IF NOT EXISTS idx_outcome_labels_label_time
                ON outcome_labels (label_time, outcome_time);
            """
        )

    def ingest_event(
        self,
        event: Mapping[str, Any],
        *,
        event_type: str = "new_event",
        related_event_id: str | None = None,
        ingested_at: str | None = None,
    ) -> dict[str, Any]:
        """Insert one event, or return the existing canonical event on replay.

        A duplicate ingestion creates one idempotent relation for auditability and
        never creates a second news event or signal requirement.
        """
        if event_type not in EVENT_TYPES:
            raise ValueError(f"unsupported event type: {event_type}")
        normalized = self._normalize_event(event, ingested_at)
        fingerprint = normalized["event_fingerprint"]
        ticker = normalized["ticker"]
        now = normalized["created_at"]

        with self.connection:
            existing = self.connection.execute(
                "SELECT * FROM news_events WHERE ticker = ? AND event_fingerprint = ?",
                (ticker, fingerprint),
            ).fetchone()

            if event_type == "duplicate" or existing is not None:
                canonical = existing
                if canonical is None and related_event_id:
                    canonical = self._fetch_event(related_event_id)
                if canonical is None:
                    raise ValueError("duplicate event must identify an existing event")
                canonical_id = str(canonical["event_id"])
                self._insert_relation(
                    canonical_id,
                    canonical_id,
                    "duplicate",
                    {"ingested_at": now, "event_fingerprint": fingerprint},
                    now,
                )
                return {
                    "event_id": canonical_id,
                    "event_type": "duplicate",
                    "status": "duplicate",
                    "inserted": False,
                    "ignored": True,
                    "trigger_signal": False,
                    "event": dict(canonical),
                }

            event_id = str(event.get("event_id") or f"ne_{uuid.uuid4().hex}")
            self.connection.execute(
                """
                INSERT INTO news_events (
                    event_id, ticker, headline, url, source, source_tier,
                    published_at, event_at, first_seen_at, retrieved_at,
                    content_hash, event_fingerprint, event_type, is_duplicate,
                    signal_required, signal_emitted, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 0, ?, ?)
                """,
                (
                    event_id,
                    normalized["ticker"],
                    normalized["headline"],
                    normalized["url"],
                    normalized["source"],
                    normalized["source_tier"],
                    normalized["published_at"],
                    normalized["event_at"],
                    normalized["first_seen_at"],
                    normalized["retrieved_at"],
                    normalized["content_hash"],
                    fingerprint,
                    event_type,
                    1 if event_type in SIGNALABLE_EVENT_TYPES else 0,
                    normalized["payload_json"],
                    now,
                ),
            )
            if related_event_id:
                self._fetch_event(related_event_id)
                self._insert_relation(related_event_id, event_id, event_type, {}, now)
            row = self._fetch_event(event_id)
            return {
                "event_id": event_id,
                "event_type": event_type,
                "status": "inserted",
                "inserted": True,
                "ignored": False,
                "trigger_signal": event_type in SIGNALABLE_EVENT_TYPES,
                "event": dict(row),
            }

    ingest_news_event = ingest_event
    insert_event = ingest_event

    def record_signal(
        self,
        event_id: str,
        *,
        signal_time: str,
        decision_time: str,
        payload: Mapping[str, Any] | None = None,
        signal_id: str | None = None,
        signal_type: str = "news",
        model: str = "",
        prompt_version: str = "",
        direction: str | None = None,
        action: str | None = None,
        confidence: float | None = None,
    ) -> dict[str, Any]:
        event = self._fetch_event(event_id)
        signal_at = _timestamp(signal_time, "signal_time")
        decided_at = _timestamp(decision_time, "decision_time")
        if parse_ts(signal_at) < parse_ts(event["first_seen_at"]):
            raise ValueError("signal_time cannot precede first_seen_at")
        if parse_ts(decided_at) < parse_ts(signal_at):
            raise ValueError("decision_time cannot precede signal_time")
        signal_id = signal_id or f"sig_{uuid.uuid4().hex}"
        payload_json = _json(payload or {})
        with self.connection:
            existing = self.connection.execute(
                """
                SELECT * FROM llm_signals
                WHERE event_id = ? AND signal_type = ? AND model = ?
                  AND prompt_version = ? AND signal_time = ?
                """,
                (event_id, signal_type, model, prompt_version, signal_at),
            ).fetchone()
            if existing is None:
                self.connection.execute(
                    """
                    INSERT INTO llm_signals (
                        signal_id, event_id, signal_type, direction, action,
                        confidence, model, prompt_version, signal_time,
                        decision_time, payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        signal_id,
                        event_id,
                        signal_type,
                        direction,
                        action,
                        confidence,
                        model,
                        prompt_version,
                        signal_at,
                        decided_at,
                        payload_json,
                        _timestamp(utc_now(), "created_at"),
                    ),
                )
                existing = self.connection.execute(
                    "SELECT * FROM llm_signals WHERE signal_id = ?", (signal_id,)
                ).fetchone()
                inserted = True
            else:
                inserted = False
            self.connection.execute(
                "UPDATE news_events SET signal_emitted = 1 WHERE event_id = ?",
                (event_id,),
            )
            return {"signal_id": str(existing["signal_id"]), "inserted": inserted, "signal": dict(existing)}

    add_signal = record_signal

    def record_tradability_observation(
        self,
        *,
        ticker: str,
        observed_at: str,
        event_id: str | None = None,
        observation: Mapping[str, Any] | None = None,
        observation_id: str | None = None,
        bid: float | None = None,
        ask: float | None = None,
        last: float | None = None,
        spread_bps: float | None = None,
        volume: float | None = None,
        is_tradable: bool | None = None,
        rejection_reason: str | None = None,
    ) -> dict[str, Any]:
        if event_id:
            self._fetch_event(event_id)
        observed = _timestamp(observed_at, "observed_at")
        payload_json = _json(observation or {})
        observation_id = observation_id or _stable_id(
            "to",
            {"event_id": event_id, "ticker": ticker.upper(), "observed_at": observed, "payload": payload_json},
        )
        values = (
            observation_id,
            event_id,
            ticker.upper(),
            observed,
            bid,
            ask,
            last,
            spread_bps,
            volume,
            None if is_tradable is None else int(is_tradable),
            rejection_reason,
            payload_json,
            _timestamp(utc_now(), "created_at"),
        )
        with self.connection:
            cursor = self.connection.execute(
                """
                INSERT OR IGNORE INTO tradability_observations (
                    observation_id, event_id, ticker, observed_at, bid, ask,
                    last, spread_bps, volume, is_tradable, rejection_reason,
                    payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            row = self.connection.execute(
                "SELECT * FROM tradability_observations WHERE observation_id = ?",
                (observation_id,),
            ).fetchone()
            return {"observation_id": str(row["observation_id"]), "inserted": cursor.rowcount == 1, "observation": dict(row)}

    add_tradability_observation = record_tradability_observation

    def record_shadow_proposal(
        self,
        *,
        ticker: str,
        action: str,
        decision_time: str,
        event_id: str | None = None,
        signal_id: str | None = None,
        direction: str | None = None,
        entry_price: float | None = None,
        stop_price: float | None = None,
        target_price: float | None = None,
        payload: Mapping[str, Any] | None = None,
        proposal_id: str | None = None,
    ) -> dict[str, Any]:
        if event_id:
            self._fetch_event(event_id)
        if signal_id:
            self._fetch_signal(signal_id)
        decided_at = _timestamp(decision_time, "decision_time")
        proposal_id = proposal_id or _stable_id(
            "sp",
            {"signal_id": signal_id, "ticker": ticker.upper(), "action": action, "decision_time": decided_at},
        )
        values = (
            proposal_id,
            event_id,
            signal_id,
            ticker.upper(),
            action,
            direction,
            decided_at,
            entry_price,
            stop_price,
            target_price,
            _json(payload or {}),
            _timestamp(utc_now(), "created_at"),
        )
        with self.connection:
            cursor = self.connection.execute(
                """
                INSERT OR IGNORE INTO shadow_proposals (
                    proposal_id, event_id, signal_id, ticker, action, direction,
                    decision_time, entry_price, stop_price, target_price,
                    payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            row = self.connection.execute(
                "SELECT * FROM shadow_proposals WHERE proposal_id = ?", (proposal_id,)
            ).fetchone()
            return {"proposal_id": str(row["proposal_id"]), "inserted": cursor.rowcount == 1, "proposal": dict(row)}

    add_shadow_proposal = record_shadow_proposal

    def record_outcome_label(
        self,
        *,
        label: str,
        horizon: str,
        outcome_time: str,
        label_time: str,
        event_id: str | None = None,
        signal_id: str | None = None,
        proposal_id: str | None = None,
        decision_time: str | None = None,
        return_pct: float | None = None,
        payload: Mapping[str, Any] | None = None,
        label_id: str | None = None,
    ) -> dict[str, Any]:
        if event_id:
            self._fetch_event(event_id)
        if signal_id:
            self._fetch_signal(signal_id)
        if proposal_id:
            self._fetch_proposal(proposal_id)
        outcome_at = _timestamp(outcome_time, "outcome_time")
        labeled_at = _timestamp(label_time, "label_time")
        decided_at = _timestamp(decision_time, "decision_time", required=False)
        if decided_at and parse_ts(outcome_at) < parse_ts(decided_at):
            raise ValueError("outcome_time cannot precede decision_time")
        label_id = label_id or _stable_id(
            "ol",
            {
                "event_id": event_id,
                "signal_id": signal_id,
                "proposal_id": proposal_id,
                "label": label,
                "horizon": horizon,
                "outcome_time": outcome_at,
            },
        )
        values = (
            label_id,
            event_id,
            signal_id,
            proposal_id,
            label,
            horizon,
            return_pct,
            outcome_at,
            labeled_at,
            decided_at,
            _json(payload or {}),
            _timestamp(utc_now(), "created_at"),
        )
        with self.connection:
            cursor = self.connection.execute(
                """
                INSERT OR IGNORE INTO outcome_labels (
                    label_id, event_id, signal_id, proposal_id, label, horizon,
                    return_pct, outcome_time, label_time, decision_time,
                    payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            row = self.connection.execute(
                "SELECT * FROM outcome_labels WHERE label_id = ?", (label_id,)
            ).fetchone()
            return {"label_id": str(row["label_id"]), "inserted": cursor.rowcount == 1, "label_record": dict(row)}

    add_outcome_label = record_outcome_label

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM news_events WHERE event_id = ?", (event_id,)).fetchone()
        return dict(row) if row else None

    def list_unproposed_signals(self, *, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT s.*, o.rejection_reason AS latest_rejection_reason
            FROM llm_signals AS s
            LEFT JOIN shadow_proposals AS p ON p.signal_id = s.signal_id
            LEFT JOIN tradability_observations AS o ON o.observation_id = (
                SELECT latest.observation_id
                FROM tradability_observations AS latest
                WHERE latest.event_id = s.event_id
                ORDER BY latest.observed_at DESC, latest.created_at DESC
                LIMIT 1
            )
            WHERE p.signal_id IS NULL
            ORDER BY s.signal_time DESC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            payload = json.loads(str(row["payload_json"]))
            result.append(
                {
                    **payload,
                    "event_id": str(row["event_id"]),
                    "signal_id": str(row["signal_id"]),
                    "signal_time": str(row["signal_time"]),
                    "latest_rejection_reason": row["latest_rejection_reason"],
                }
            )
        return result

    def list_events(
        self,
        *,
        ticker: str | None = None,
        event_type: str | None = None,
        pending_signal_only: bool = False,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if ticker is not None:
            clauses.append("ticker = ?")
            parameters.append(ticker.upper())
        duplicate_query = event_type == "duplicate"
        if event_type is not None:
            if event_type not in EVENT_TYPES:
                raise ValueError(f"unsupported event type: {event_type}")
            if event_type == "duplicate":
                clauses.append("event_id IN (SELECT source_event_id FROM event_relations WHERE relation_type = 'duplicate')")
            else:
                clauses.append("event_type = ?")
                parameters.append(event_type)
        if pending_signal_only:
            clauses.extend(["signal_required = 1", "signal_emitted = 0"])
        query = "SELECT * FROM news_events"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY first_seen_at, event_id"
        rows = [dict(row) for row in self.connection.execute(query, parameters).fetchall()]
        if duplicate_query:
            for row in rows:
                row["canonical_event_id"] = row["event_id"]
                row["ingestion_type"] = "duplicate"
                row["event_type"] = "duplicate"
        return rows

    def pending_signal_events(self) -> list[dict[str, Any]]:
        return self.list_events(pending_signal_only=True)

    def list_relations(
        self,
        *,
        event_id: str | None = None,
        relation_type: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if event_id:
            clauses.append("(source_event_id = ? OR target_event_id = ?)")
            parameters.extend([event_id, event_id])
        if relation_type:
            if relation_type not in EVENT_RELATION_TYPES:
                raise ValueError(f"unsupported relation type: {relation_type}")
            clauses.append("relation_type = ?")
            parameters.append(relation_type)
        query = "SELECT * FROM event_relations"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY relation_id"
        return [dict(row) for row in self.connection.execute(query, parameters).fetchall()]

    def _insert_relation(
        self,
        source_event_id: str,
        target_event_id: str,
        relation_type: str,
        payload: Mapping[str, Any],
        created_at: str,
    ) -> None:
        self.connection.execute(
            """
            INSERT OR IGNORE INTO event_relations (
                source_event_id, target_event_id, relation_type, created_at, payload_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (source_event_id, target_event_id, relation_type, created_at, _json(payload)),
        )

    def _fetch_event(self, event_id: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM news_events WHERE event_id = ?", (event_id,)).fetchone()
        if row is None:
            raise ValueError(f"unknown event_id: {event_id}")
        return row

    def _fetch_signal(self, signal_id: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM llm_signals WHERE signal_id = ?", (signal_id,)).fetchone()
        if row is None:
            raise ValueError(f"unknown signal_id: {signal_id}")
        return row

    def _fetch_proposal(self, proposal_id: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM shadow_proposals WHERE proposal_id = ?", (proposal_id,)).fetchone()
        if row is None:
            raise ValueError(f"unknown proposal_id: {proposal_id}")
        return row

    @staticmethod
    def _normalize_event(event: Mapping[str, Any], ingested_at: str | None) -> dict[str, Any]:
        ticker = str(event.get("ticker") or "").strip().upper()
        headline = str(event.get("headline") or "").strip()
        source = str(event.get("source") or "unknown").strip() or "unknown"
        if not headline:
            raise ValueError("event headline is required")
        published_at = _timestamp(event.get("published_at"), "published_at")
        first_seen_at = _timestamp(event.get("first_seen_at") or ingested_at or utc_now(), "first_seen_at")
        if parse_ts(first_seen_at) < parse_ts(published_at):
            raise ValueError("first_seen_at cannot precede published_at")
        event_at = _timestamp(event.get("event_at"), "event_at", required=False)
        retrieved_at = _timestamp(event.get("retrieved_at"), "retrieved_at", required=False)
        url = _canonical_url(str(event.get("url") or ""))
        content_hash = str(event.get("content_hash") or "").strip()
        if not content_hash:
            content_hash = _sha256(
                _json({"headline": headline, "url": url, "highlights": event.get("highlights", [])})
            )
        fingerprint = str(event.get("event_fingerprint") or event.get("fingerprint") or "").strip()
        if not fingerprint:
            fingerprint = _sha256(
                _json(
                    {
                        "ticker": ticker,
                        "url": url,
                        "published_at": published_at,
                        "content_hash": content_hash,
                    }
                )
            )
        created_at = _timestamp(ingested_at or first_seen_at, "created_at")
        return {
            "ticker": ticker,
            "headline": headline,
            "source": source,
            "source_tier": event.get("source_tier"),
            "published_at": published_at,
            "event_at": event_at,
            "first_seen_at": first_seen_at,
            "retrieved_at": retrieved_at,
            "url": url,
            "content_hash": content_hash,
            "event_fingerprint": fingerprint,
            "payload_json": _json(dict(event)),
            "created_at": created_at,
        }


NewsDriftEventStore = NewsEventStore


def _timestamp(value: Any, field: str, *, required: bool = True) -> str | None:
    if value is None or str(value).strip() == "":
        if required:
            raise ValueError(f"{field} is required")
        return None
    try:
        return parse_ts(str(value)).isoformat()
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {field}: {value}") from exc


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _stable_id(prefix: str, value: Mapping[str, Any]) -> str:
    return f"{prefix}_{_sha256(_json(value))[:32]}"


def _canonical_url(url: str) -> str:
    if not url:
        return ""
    parts = urlsplit(url.strip())
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), "", ""))
