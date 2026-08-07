from __future__ import annotations

import sqlite3

from scripts.news_drift.event_store import NewsEventStore


PUBLISHED = "2026-08-04T14:30:00Z"
FIRST_SEEN = "2026-08-04T14:31:00Z"
SIGNAL = "2026-08-04T14:32:00Z"
DECISION = "2026-08-04T14:33:00Z"
OUTCOME = "2026-08-05T20:00:00Z"
LABELED = "2026-08-05T20:01:00Z"


def event(*, fingerprint: str = "event-1", headline: str = "Company reports a new contract") -> dict[str, str]:
    return {
        "ticker": "TEST",
        "headline": headline,
        "published_at": PUBLISHED,
        "first_seen_at": FIRST_SEEN,
        "source": "sec.gov",
        "source_tier": 1,
        "url": "https://sec.gov/filing/1",
        "event_fingerprint": fingerprint,
    }


def test_schema_contains_required_shadow_tables(tmp_path):
    with NewsEventStore(tmp_path / "news.sqlite") as store:
        names = {
            row["name"]
            for row in store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert {
        "news_events",
        "event_relations",
        "llm_signals",
        "tradability_observations",
        "outcome_labels",
        "shadow_proposals",
    } <= names


def test_ingestion_is_idempotent_and_duplicate_is_queryable(tmp_path):
    with NewsEventStore(tmp_path / "news.sqlite") as store:
        first = store.ingest_event(event())
        second = store.ingest_event(event())

        assert first["inserted"] is True
        assert second["inserted"] is False
        assert second["status"] == "duplicate"
        assert len(store.list_events()) == 1
        assert len(store.list_events(event_type="duplicate")) == 1
        assert len(store.list_relations(relation_type="duplicate")) == 1
        assert store.pending_signal_events()[0]["event_id"] == first["event_id"]


def test_material_update_and_contradiction_create_related_signalable_events(tmp_path):
    with NewsEventStore(tmp_path / "news.sqlite") as store:
        original = store.ingest_event(event()).get("event_id")
        update = store.ingest_event(
            event(fingerprint="event-2", headline="Company raises contract guidance"),
            event_type="material_update",
            related_event_id=original,
        )
        contradiction = store.ingest_event(
            event(fingerprint="event-3", headline="Company disputes the earlier guidance"),
            event_type="contradiction",
            related_event_id=update["event_id"],
        )

        relations = store.list_relations()
        assert {(row["relation_type"], row["source_event_id"], row["target_event_id"]) for row in relations} == {
            ("material_update", original, update["event_id"]),
            ("contradiction", update["event_id"], contradiction["event_id"]),
        }
        assert {row["event_id"] for row in store.pending_signal_events()} == {
            original,
            update["event_id"],
            contradiction["event_id"],
        }

        signal = store.record_signal(
            update["event_id"],
            signal_time=SIGNAL,
            decision_time=DECISION,
            model="mock",
            prompt_version="v1",
            payload={"direction": "positive"},
        )
        assert signal["inserted"] is True
        assert {row["event_id"] for row in store.pending_signal_events()} == {
            original,
            contradiction["event_id"],
        }


def test_timestamps_survive_signal_proposal_and_outcome_label(tmp_path):
    with NewsEventStore(tmp_path / "news.sqlite") as store:
        event_result = store.ingest_event(event())
        event_id = event_result["event_id"]
        signal = store.record_signal(
            event_id,
            signal_time=SIGNAL,
            decision_time=DECISION,
            model="mock",
            prompt_version="v1",
        )
        proposal = store.record_shadow_proposal(
            ticker="TEST",
            action="buy",
            direction="positive",
            decision_time=DECISION,
            event_id=event_id,
            signal_id=signal["signal_id"],
            entry_price=10.25,
        )
        label = store.record_outcome_label(
            event_id=event_id,
            signal_id=signal["signal_id"],
            proposal_id=proposal["proposal_id"],
            label="profitable",
            horizon="1d",
            return_pct=1.2,
            outcome_time=OUTCOME,
            label_time=LABELED,
            decision_time=DECISION,
        )

        saved_event = store.get_event(event_id)
        saved_signal = store.connection.execute(
            "SELECT * FROM llm_signals WHERE signal_id = ?", (signal["signal_id"],)
        ).fetchone()
        saved_label = label["label_record"]
        assert saved_event["published_at"] == "2026-08-04T14:30:00+00:00"
        assert saved_event["first_seen_at"] == "2026-08-04T14:31:00+00:00"
        assert saved_signal["signal_time"] == "2026-08-04T14:32:00+00:00"
        assert saved_signal["decision_time"] == "2026-08-04T14:33:00+00:00"
        assert saved_label["outcome_time"] == "2026-08-05T20:00:00+00:00"
        assert saved_label["label_time"] == "2026-08-05T20:01:00+00:00"


def test_store_uses_sqlite_connection_without_external_database_dependency(tmp_path):
    with NewsEventStore(":memory:") as store:
        assert isinstance(store.connection, sqlite3.Connection)
        assert store.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
