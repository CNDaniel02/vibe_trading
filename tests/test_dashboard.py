from __future__ import annotations

import json

from scripts.dashboard.paper_dashboard import build_dashboard_state, make_handler


def test_dashboard_state_explains_deterministic_rejection_and_paper_boundary(paper_root):
    baseline = {
        "event": "baseline_decision",
        "decision": {
            "snapshot_id": "dash_1",
            "ticker": "AAPL",
            "action": "no_trade",
            "regime": {"status": "risk_on", "eligible": True, "reasons": []},
            "technical": {
                "quote_valid": True,
                "quote_reason": "quote ok",
                "relative_strength_20d": 2.1,
                "price_change_5d_pct": -0.5,
                "volume_ratio": 1.0,
            },
        },
        "snapshot": {"market_session": "regular"},
    }
    (paper_root / "logs" / "decisions.jsonl").write_text(json.dumps(baseline) + "\n", encoding="utf-8")
    state = build_dashboard_state(paper_root)
    assert state["mode"] == {"paper": True, "live_trading": False}
    assert state["strategy_modes"]["weighted_relative_strength_v2"] == "shadow_only"
    assert state["safety"]["allow_options"] is True
    assert state["safety"]["options_risk"]["allow_sell_to_open"] is False
    assert state["safety"]["options_risk"]["allow_margin"] is False
    assert state["safety"]["allow_fractional_shares"] is True
    assert state["candidates"][0]["ticker"] == "AAPL"
    assert "5-day price change is below 0.5" in state["candidates"][0]["reasons"]
    assert state["ai_instrument_allocator"]["metrics"] is None


def test_dashboard_handler_exposes_only_read_routes(paper_root):
    handler = make_handler(paper_root)
    assert handler.__name__ == "DashboardHandler"
    assert not hasattr(handler, "do_POST")


def test_dashboard_exposes_sanitized_catalyst_decision(paper_root):
    catalyst = {
        "ticker": "IONQ",
        "final_action": "buy",
        "instrument": "equity",
        "risk_approved": True,
        "risk_reason": "approved",
        "model_calls": 3,
        "ranking": {"score": 0.8, "direction": "bullish", "rationale": "fresh event", "risk_flags": []},
        "bull_news": {
            "catalyst_summary": "Material contract",
            "direction": "positive",
            "event_time": "2026-07-06T15:00:00Z",
            "source_urls": ["https://example.com/event"],
            "data_gaps": [],
        },
        "challenge": {"recommendation": "proceed", "veto_recommended": False, "objections": [], "missing_evidence": []},
        "decision": {
            "thesis": "Material contract may reprice the equity.",
            "supporting_evidence": ["Company release"],
            "contrary_evidence": [],
            "confidence": 0.8,
            "no_trade_reason": None,
        },
        "reasoning_content": "must never be surfaced",
    }
    (paper_root / "logs" / "catalyst_decisions.jsonl").write_text(json.dumps(catalyst) + "\n", encoding="utf-8")
    state = build_dashboard_state(paper_root)
    visible = state["catalyst_decisions"][0]
    assert visible["ticker"] == "IONQ"
    assert visible["decision"]["thesis"] == "Material contract may reprice the equity."
    assert "reasoning_content" not in visible


def test_dashboard_beginner_summary_separates_loss_from_runtime_failures(
    paper_root,
):
    (paper_root / "state" / "daily_counters.json").write_text(
        json.dumps(
            {
                "date": "2026-07-29",
                "trades": 2,
                "equity_trades": 2,
                "option_trades": 0,
                "daily_realized_pnl": -5.25,
            }
        ),
        encoding="utf-8",
    )
    (paper_root / "state" / "paper_account.json").write_text(
        json.dumps(
            {
                "cash": 1994.75,
                "initial_cash": 2000,
                "realized_pnl": -5.25,
                "updated_at": "2026-07-29T19:50:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    (paper_root / "state" / "paper_orders.json").write_text(
        json.dumps(
            {
                "exit-1": {
                    "order_id": "exit-1",
                    "decision_id": "exit:AAPL",
                    "symbol": "AAPL",
                    "side": "sell",
                    "order_type": "market",
                    "quantity": 1,
                    "limit_price": None,
                    "status": "filled",
                    "filled_quantity": 1,
                    "average_fill_price": 94.75,
                    "thesis": "mandatory pre-close flatten",
                }
            }
        ),
        encoding="utf-8",
    )
    trade = {
        "event": "trade_closed",
        "instrument": "equity",
        "symbol": "AAPL",
        "quantity": 1,
        "entry_time": "2026-07-29T18:00:00+00:00",
        "exit_time": "2026-07-29T19:50:00+00:00",
        "entry_price": 100,
        "exit_price": 94.75,
        "realized_pnl": -5.25,
        "return_pct": -5.25,
        "holding_minutes": 110,
        "outcome": "loss",
        "exit_order_id": "exit-1",
    }
    (paper_root / "logs" / "trade_journal.jsonl").write_text(
        json.dumps(trade) + "\n",
        encoding="utf-8",
    )
    ai_failure = {
        "event": "ai_gated_cycle_failed_closed",
        "stage": "model_ranking",
        "ts": "2026-07-29T15:00:00+00:00",
    }
    (paper_root / "logs" / "ai_gated_cycles.jsonl").write_text(
        json.dumps(ai_failure) + "\n",
        encoding="utf-8",
    )
    option_failure = {
        "ticker": "QQQ",
        "diagnostics": {
            "rejections": {
                "future option quote would create lookahead": 20
            }
        },
        "ts": "2026-07-29T16:00:00+00:00",
    }
    (paper_root / "logs" / "option_selection_diagnostics.jsonl").write_text(
        json.dumps(option_failure) + "\n",
        encoding="utf-8",
    )

    summary = build_dashboard_state(paper_root)["beginner_summary"]
    assert summary["session_date"] == "2026-07-29"
    assert summary["day"]["realized_pnl"] == -5.25
    assert summary["day"]["losses"] == 1
    assert summary["day"]["trades"][0]["exit_reason"] == "mandatory pre-close flatten"
    assert summary["strategy_lines"]["ai"]["status"] == "failed_closed"
    assert summary["strategy_lines"]["options"]["status"] == "validation_error"
    assert {issue["code"] for issue in summary["issues"]} == {
        "ai_structured_output_failed",
        "option_quote_observation_time",
    }


def test_dashboard_handles_failed_forward_job_without_output(paper_root):
    (paper_root / "logs" / "audit.jsonl").write_text(
        json.dumps(
            {
                "event": "forward_cycle_skipped",
                "ts": "2026-07-04T21:00:00+00:00",
                "clock": {"market_session": "after_hours"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (paper_root / "state" / "runtime_heartbeat.json").write_text(
        json.dumps(
            {
                "last_heartbeat_at": "2026-07-29T15:00:00+00:00",
                "status": "degraded",
                "payload": {
                    "latest_jobs": {
                        "forward": {
                            "status": "failed",
                            "output": None,
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    summary = build_dashboard_state(paper_root)["beginner_summary"]

    assert summary["service"]["market_session"] == "after_hours"


def test_dashboard_handles_null_runtime_payload_and_allocator_state(paper_root):
    (paper_root / "state" / "runtime_heartbeat.json").write_text(
        json.dumps({"payload": None}),
        encoding="utf-8",
    )
    state_dir = (
        paper_root
        / "state"
        / "strategy_sleeves"
        / "ai_instrument_allocator_v1"
    )
    state_dir.mkdir(parents=True)
    (state_dir / "paper_account.json").write_text("null", encoding="utf-8")

    state = build_dashboard_state(paper_root)

    assert state["beginner_summary"]["service"]["market_session"] is None
    assert state["ai_instrument_allocator"]["account"] is None
    assert (
        state["ai_instrument_allocator"]["metrics"]["metrics_available"]
        is False
    )


def test_dashboard_separates_ten_thousand_allocator_and_counterfactual(
    paper_root,
):
    namespace = "ai_instrument_allocator_v1"
    state_dir = paper_root / "state" / "strategy_sleeves" / namespace
    log_dir = paper_root / "logs" / "strategy_sleeves" / namespace
    state_dir.mkdir(parents=True)
    log_dir.mkdir(parents=True)
    (state_dir / "paper_account.json").write_text(
        json.dumps(
            {
                "cash": 9749.95,
                "initial_cash": 10000,
                "realized_pnl": 0,
                "updated_at": "2026-07-13T15:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    for name in (
        "paper_positions.json",
        "paper_orders.json",
        "paper_option_positions.json",
        "paper_option_orders.json",
    ):
        (state_dir / name).write_text("{}", encoding="utf-8")
    (state_dir / "daily_counters.json").write_text(
        json.dumps({"date": "2026-07-13", "trades": 0}),
        encoding="utf-8",
    )
    (state_dir / "allocator_plans.json").write_text(
        json.dumps(
            {
                "plan-aapl": {
                    "plan_id": "plan-aapl",
                    "ticker": "AAPL",
                    "status": "active",
                    "signal": {"horizon": "next_close"},
                }
            }
        ),
        encoding="utf-8",
    )
    (state_dir / "position_mandates.json").write_text(
        json.dumps(
            {
                "equity:AAPL": {
                    "exposure_id": "equity:AAPL",
                    "ticker": "AAPL",
                    "instrument_type": "equity",
                    "horizon": "next_close",
                    "status": "open",
                    "planned_exit_at": "2026-07-14T19:50:00+00:00",
                }
            }
        ),
        encoding="utf-8",
    )
    allocation = {
        "allocation_id": "allocation-aapl",
        "decision_time": "2026-07-13T15:00:00+00:00",
        "status": "selected",
        "selected_instrument": {
            "instrument_type": "equity",
            "ticker": "AAPL",
            "quantity": 2.5,
            "entry_price": 100.02,
            "conservative_net_return_pct": 0.01,
        },
        "counterfactual_2000": {
            "affordable": True,
            "max_affordable_quantity": 4.999,
            "risk_pct_of_nav": 0.01,
            "rejection_reason": None,
            "alternative_instrument_considered": False,
        },
        "probability_ev_available": False,
    }
    (log_dir / "allocations.jsonl").write_text(
        json.dumps(allocation) + "\n",
        encoding="utf-8",
    )
    (log_dir / "decisions.jsonl").write_text(
        json.dumps(
            {
                "ticker": "AAPL",
                "stage": "overnight",
                "signal": {
                    "horizon": "next_close",
                    "probability_status": "uncalibrated",
                    "thesis": "Fixture thesis",
                },
                "reasoning_content": "must never be exposed",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (log_dir / "short_equity_counterfactual.jsonl").write_text(
        json.dumps(
            {
                "benchmark_name": "short_equity_counterfactual",
                "ticker": "MSFT",
                "creates_order": False,
                "enters_account": False,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    allocator = build_dashboard_state(paper_root)["ai_instrument_allocator"]

    assert allocator["account"]["initial_cash"] == 10000
    assert allocator["metrics"]["initial_cash"] == 10000
    assert allocator["latest_allocation"]["selected_instrument"]["ticker"] == "AAPL"
    assert allocator["latest_allocation"]["counterfactual_2000"]["affordable"] is True
    assert allocator["latest_allocation"]["probability_ev_available"] is False
    assert allocator["mandates"][0]["horizon"] == "next_close"
    assert allocator["short_equity_counterfactual"]["creates_order"] is False
    assert "reasoning_content" not in allocator["decisions"][0]
