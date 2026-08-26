from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest


def _snapshot(root: Path, *, ticker: str, decision_time: str) -> dict[str, str]:
    envelope = {
        "snapshot_type": f"allocator-intraday-{ticker}",
        "decision_time": decision_time,
        "retrieved_at": decision_time,
        "payload": {
            "candidate": {"ticker": ticker},
            "events": [],
            "source_metadata": [],
        },
    }
    serialized = json.dumps(envelope, separators=(",", ":"), sort_keys=True)
    snapshot_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    envelope["snapshot_hash"] = snapshot_hash
    path = root / "logs" / "ai_instrument_allocator_snapshots" / f"{ticker}-{snapshot_hash[:12]}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(envelope, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"path": str(path), "snapshot_hash": snapshot_hash}


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
        encoding="utf-8",
    )


def test_allocator_policy_replay_is_point_in_time_read_only_and_fail_closed(
    tmp_path: Path,
) -> None:
    from scripts.replay.allocator_policy_replay import run_allocator_policy_replay

    refs = {
        ticker: _snapshot(
            tmp_path,
            ticker=ticker,
            decision_time=f"2026-07-13T1{index}:00:00+00:00",
        )
        for index, ticker in enumerate(("AAPL", "MSFT", "NVDA"), start=5)
    }
    log_dir = tmp_path / "logs" / "strategy_sleeves" / "ai_instrument_allocator_v1"
    _write_jsonl(
        log_dir / "decisions.jsonl",
        [
            {
                "ts": "2026-07-13T15:00:10+00:00",
                "ticker": "AAPL",
                "stage": "intraday",
                "evidence_snapshot": refs["AAPL"],
                "challenge": {"veto_recommended": True},
                "signal": {"action": "no_trade", "no_trade_reason": "legacy veto"},
            },
            {
                "ts": "2026-07-13T16:00:10+00:00",
                "ticker": "MSFT",
                "stage": "intraday",
                "evidence_snapshot": refs["MSFT"],
                "challenge": {
                    "hard_veto": False,
                    "hard_veto_reasons": [],
                    "soft_concerns": [
                        {"code": "partial_price_in", "detail": "Partly priced in."}
                    ],
                },
                "signal": {"action": "watch", "watch_reason": "Thesis incomplete."},
            },
            {
                "ts": "2026-07-13T17:00:10+00:00",
                "ticker": "NVDA",
                "stage": "intraday",
                "evidence_snapshot": refs["NVDA"],
                "challenge": {
                    "hard_veto": False,
                    "hard_veto_reasons": [],
                    "soft_concerns": [],
                },
                "signal": {"action": "propose_trade", "entry_now": True},
            },
        ],
    )
    _write_jsonl(
        log_dir / "cycles.jsonl",
        [
            {
                "ts": "2026-07-13T17:01:00+00:00",
                "stage": "intraday",
                "funnel": {"candidate_discovery": 3, "ranking_input": 3, "deep_research": 3},
                "skipped": [],
                "plans": [],
                "watches": [],
                "executions": [],
                "paper_orders_created": 0,
                "live_order_tools_called": False,
            }
        ],
    )
    state_dir = tmp_path / "state" / "strategy_sleeves" / "ai_instrument_allocator_v1"
    state_dir.mkdir(parents=True)
    (state_dir / "paper_orders.json").write_text("{}\n", encoding="utf-8")
    before = (state_dir / "paper_orders.json").read_bytes()

    report = run_allocator_policy_replay(tmp_path, hours=48)

    assert report["asof"] == "2026-07-13T17:01:00+00:00"
    assert report["snapshot_integrity"]["checked"] == 3
    assert report["snapshot_integrity"]["valid"] == 3
    assert report["snapshot_integrity"]["invalid"] == 0
    assert report["point_in_time"]["violation_count"] == 0
    assert report["old_policy"]["proposals"] == 1
    assert report["old_policy"]["watch"] == 0
    assert report["new_policy"]["proposals"] == 1
    assert report["new_policy"]["watch"] == 1
    assert report["comparison"]["proposal_delta"] == 0
    assert report["comparison"]["watch_delta"] == 1
    assert report["comparison"]["legacy_ambiguous_veto_count"] == 1
    assert report["comparison"]["estimated_avoidable_rank_only_cooldowns"] == 0
    assert report["point_in_time"]["replayable_snapshot_count"] == 3
    assert report["point_in_time"]["excluded_snapshot_count"] == 0
    assert report["model_calls"] == 0
    assert report["historical_orders_created"] == 0
    assert (state_dir / "paper_orders.json").read_bytes() == before


def test_allocator_policy_replay_rejects_tampered_snapshot(tmp_path: Path) -> None:
    from scripts.replay.allocator_policy_replay import run_allocator_policy_replay

    ref = _snapshot(
        tmp_path,
        ticker="AAPL",
        decision_time="2026-07-13T15:00:00+00:00",
    )
    path = Path(ref["path"])
    value = json.loads(path.read_text(encoding="utf-8"))
    value["payload"]["candidate"]["ticker"] = "MSFT"
    path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(ValueError, match="snapshot hash mismatch"):
        run_allocator_policy_replay(tmp_path, hours=48)


def test_allocator_policy_replay_deduplicates_timestamp_occurrences_and_counts_option_rejections(
    tmp_path: Path,
) -> None:
    from scripts.replay.allocator_policy_replay import run_allocator_policy_replay

    envelope = {
        "snapshot_type": "allocator-intraday-XPEV",
        "decision_time": "2026-07-13T15:00:00+00:00",
        "retrieved_at": "2026-07-13T15:00:00+00:00",
        "payload": {
            "candidate": {
                "ticker": "XPEV",
                "market_context": {
                    "quote": {"asof": "2026-07-13T15:00:01+00:00"}
                },
            },
            "events": [
                {
                    "published_at": "2026-07-13T14:00:00+00:00",
                    "first_seen_at": "2026-07-13T15:00:02+00:00",
                    "retrieved_at": "2026-07-13T15:00:03+00:00",
                }
            ],
            "new_events": [
                {
                    "published_at": "2026-07-13T14:00:00+00:00",
                    "first_seen_at": "2026-07-13T15:00:02+00:00",
                    "retrieved_at": "2026-07-13T15:00:03+00:00",
                }
            ],
            "source_metadata": [
                {"retrieved_at": "2026-07-13T15:00:04+00:00"}
            ],
            "agent_snapshot": {
                "market_data": {
                    "quote": {"asof": "2026-07-13T15:00:01+00:00"}
                },
                "available_news": [
                    {
                        "published_at": "2026-07-13T14:00:00+00:00",
                        "first_seen_at": "2026-07-13T15:00:02+00:00",
                        "retrieved_at": "2026-07-13T15:00:03+00:00",
                    }
                ],
            },
        },
    }
    serialized = json.dumps(envelope, separators=(",", ":"), sort_keys=True)
    snapshot_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    envelope["snapshot_hash"] = snapshot_hash
    snapshot_path = (
        tmp_path
        / "logs"
        / "ai_instrument_allocator_snapshots"
        / f"XPEV-{snapshot_hash[:12]}.json"
    )
    snapshot_path.parent.mkdir(parents=True)
    snapshot_path.write_text(json.dumps(envelope), encoding="utf-8")

    log_dir = tmp_path / "logs" / "strategy_sleeves" / "ai_instrument_allocator_v1"
    _write_jsonl(
        log_dir / "decisions.jsonl",
        [
            {
                "ts": "2026-07-13T15:00:30+00:00",
                "ticker": "XPEV",
                "evidence_snapshot": {
                    "path": str(snapshot_path),
                    "snapshot_hash": snapshot_hash,
                },
                "challenge": {
                    "hard_veto": False,
                    "hard_veto_reasons": [],
                    "soft_concerns": [],
                },
                "signal": {"action": "propose_trade"},
            }
        ],
    )
    _write_jsonl(
        log_dir / "cycles.jsonl",
        [
            {
                "ts": "2026-07-13T15:01:01+00:00",
                "plans": [
                    {
                        "plan_id": "plan-xpev",
                        "evidence_snapshot": {
                            "path": str(snapshot_path),
                            "snapshot_hash": snapshot_hash,
                        },
                    }
                ],
                "executions": [
                    {
                        "allocation": {"plan_id": "plan-xpev"},
                        "order": {"order_id": "order-xpev"},
                    }
                ],
                "skipped": [],
                "paper_orders_created": 1,
            }
        ],
    )
    _write_jsonl(
        log_dir / "allocations.jsonl",
        [
            {
                "ts": "2026-07-13T15:01:00+00:00",
                "plan_id": "plan-xpev",
                "status": "no_trade",
                "reason": "signed return direction is neutral or insufficiently dominant",
                "option_candidate_diagnostics": {
                    "rejections": {
                        "option spread too wide": 14,
                        "stale option quote": 6,
                        "premium above deterministic budget": 2,
                    }
                },
            }
        ],
    )
    _write_jsonl(
        log_dir / "paper_fills.jsonl",
        [
            {
                "ts": "2026-07-13T15:00:55+00:00",
                "fill": {"order_id": "order-xpev"},
            }
        ],
    )

    report = run_allocator_policy_replay(tmp_path, hours=48)

    assert report["point_in_time"]["violation_count"] == 4
    assert report["point_in_time"]["by_field"] == {
        "candidate.quote.asof": 1,
        "news.first_seen_at": 1,
        "news.retrieved_at": 1,
        "source.retrieved_at": 1,
    }
    assert report["point_in_time"]["replayable_snapshot_count"] == 0
    assert report["point_in_time"]["excluded_snapshot_count"] == 1
    assert report["point_in_time"]["excluded_decision_count"] == 1
    assert report["point_in_time"]["excluded_allocation_count"] == 1
    assert report["observed_audit_funnel"]["candidates"] == 1
    assert report["observed_audit_funnel"]["proposals"] == 1
    assert report["observed_audit_funnel"]["allocations"] == 1
    assert report["observed_audit_funnel"]["paper_orders"] == 1
    assert report["observed_audit_funnel"]["paper_fills"] == 1
    assert report["old_policy"]["candidates"] == 0
    assert report["old_policy"]["proposals"] == 0
    assert report["old_policy"]["allocations"] == 0
    assert report["old_policy"]["paper_orders"] == 0
    assert report["old_policy"]["paper_fills"] == 0
    assert report["blockers"]["direction_gate"] == 1
    assert report["blockers"]["spread_liquidity"] == 20
    assert report["blockers"]["option_affordability"] == 2


def test_allocator_policy_replay_uses_exact_candidate_links_when_available(
    tmp_path: Path,
) -> None:
    from scripts.replay.allocator_policy_replay import run_allocator_policy_replay

    ref = _snapshot(
        tmp_path,
        ticker="AAPL",
        decision_time="2026-07-13T15:00:00+00:00",
    )
    log_dir = tmp_path / "logs" / "strategy_sleeves" / "ai_instrument_allocator_v1"
    _write_jsonl(
        log_dir / "cycles.jsonl",
        [
            {
                "ts": "2026-07-13T15:01:00+00:00",
                "funnel": {
                    "candidate_records": [
                        {
                            "ticker": "AAPL",
                            "evidence_snapshot": ref,
                            "ranking_entered": True,
                            "deep_research": True,
                            "decision_outcome": "watch",
                            "cooldown_transition_id": "cooldown-1",
                        }
                    ]
                },
                "skipped": [],
                "plans": [],
                "watches": [],
                "executions": [],
                "paper_orders_created": 0,
            }
        ],
    )
    _write_jsonl(
        log_dir / "decisions.jsonl",
        [
            {
                "ts": "2026-07-13T15:00:30+00:00",
                "ticker": "AAPL",
                "evidence_snapshot": ref,
                "challenge": {
                    "hard_veto": False,
                    "hard_veto_reasons": [],
                    "soft_concerns": [],
                },
                "signal": {"action": "watch"},
            }
        ],
    )

    report = run_allocator_policy_replay(tmp_path, hours=48)

    assert report["old_policy"]["candidates"] == 1
    assert report["old_policy"]["ranking_input"] == 1
    assert report["old_policy"]["deep_research"] == 1
    assert report["old_policy"]["watch"] == 0
    assert report["new_policy"]["watch"] == 1
    assert report["comparison"]["candidate_linkage_complete"] is True
