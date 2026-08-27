from __future__ import annotations

import json
from pathlib import Path
import re
import sqlite3

import pytest
import yaml

import scripts.dashboard.paper_dashboard as dashboard

from scripts.dashboard.paper_dashboard import (
    _BEGINNER_PAGE,
    _build_trade_funnel,
    _read_jsonl,
    build_dashboard_state,
    make_handler,
)


def test_dashboard_jsonl_reader_uses_bounded_tail_and_keeps_last_valid_records(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "large.jsonl"
    records = [
        {"index": 1, "text": "old"},
        {"index": 2, "text": "上涨"},
        {"index": 3, "text": "最新"},
    ]
    path.write_text(
        "".join(
            json.dumps(record, ensure_ascii=False) + "\n" for record in records
        )
        + '{"index": 4',
        encoding="utf-8",
    )

    def fail_full_read(*_args, **_kwargs):
        raise AssertionError("dashboard must not read the whole JSONL file")

    monkeypatch.setattr(type(path), "read_text", fail_full_read)

    assert _read_jsonl(path, limit=2) == records[-2:]


def test_dashboard_jsonl_reader_returns_empty_for_non_positive_limit(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text('{"event":"ignored"}\n', encoding="utf-8")

    assert _read_jsonl(path, limit=0) == []


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
    assert state["mode"] == {
        "paper": True,
        "live_readonly": False,
        "live_trading": False,
    }
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
    assert hasattr(handler, "do_GET")
    assert hasattr(handler, "do_HEAD")
    assert hasattr(handler, "do_OPTIONS")
    for method in ("do_POST", "do_PUT", "do_PATCH", "do_DELETE"):
        assert not hasattr(handler, method)


def test_dashboard_options_route_advertises_only_read_methods(paper_root):
    handler_type = make_handler(paper_root)
    handler = object.__new__(handler_type)
    responses = []
    headers = []
    handler.send_response = lambda status: responses.append(status)
    handler.send_header = lambda name, value: headers.append((name, value))
    handler.end_headers = lambda: None

    handler.do_OPTIONS()

    assert responses == [204]
    assert ("Allow", "GET, HEAD, OPTIONS") in headers


def test_dashboard_page_has_five_accessible_operational_tabs():
    expected_tabs = {
        "overview": "总览",
        "portfolio": "持仓与订单",
        "strategies": "策略表现",
        "ai": "AI 决策",
        "health": "系统健康",
    }

    assert 'role="tablist"' in _BEGINNER_PAGE
    assert len(re.findall(r'<button[^>]+role="tab"', _BEGINNER_PAGE)) == len(
        expected_tabs
    )
    assert len(re.findall(r'<section[^>]+role="tabpanel"', _BEGINNER_PAGE)) == len(
        expected_tabs
    )
    for tab_id, label in expected_tabs.items():
        assert f'id="tab-{tab_id}"' in _BEGINNER_PAGE
        assert f'aria-controls="panel-{tab_id}"' in _BEGINNER_PAGE
        assert f'id="panel-{tab_id}"' in _BEGINNER_PAGE
        assert label in _BEGINNER_PAGE
    assert "location.hash" in _BEGINNER_PAGE
    assert 'addEventListener("keydown"' in _BEGINNER_PAGE


def test_dashboard_page_uses_visibility_aware_fifteen_second_polling():
    assert "const REFRESH_INTERVAL_MS=15000" in _BEGINNER_PAGE
    assert "document.hidden" in _BEGINNER_PAGE
    assert 'addEventListener("visibilitychange"' in _BEGINNER_PAGE
    assert "setInterval(refresh,5000)" not in _BEGINNER_PAGE


def test_dashboard_page_keeps_explicit_paper_only_boundary():
    assert "仅使用假钱模拟" in _BEGINNER_PAGE
    assert "不会调用真实下单工具" in _BEGINNER_PAGE
    assert "模拟交易控制台" in _BEGINNER_PAGE
    assert "function paperOnlyMode(mode)" in _BEGINNER_PAGE
    assert "live_readonly" in _BEGINNER_PAGE


def test_dashboard_page_renders_all_operational_detail_views():
    for function_name in (
        "renderPortfolio",
        "renderStrategies",
        "renderAiDecisions",
        "renderHealth",
    ):
        assert f"function {function_name}(state)" in _BEGINNER_PAGE
        assert f"{function_name}(state)" in _BEGINNER_PAGE
    assert 'data-record-type="position"' in _BEGINNER_PAGE
    assert 'data-record-type="order"' in _BEGINNER_PAGE
    assert "只管理旧仓" in _BEGINNER_PAGE
    assert "确定性 Python 风控" in _BEGINNER_PAGE
    assert "当前组件状态" in _BEGINNER_PAGE
    assert "最近交易日历史事件" in _BEGINNER_PAGE


def test_dashboard_page_keeps_private_reasoning_out_of_rendering():
    assert "reasoning_content" not in _BEGINNER_PAGE
    assert "private chain-of-thought" not in _BEGINNER_PAGE.lower()


def test_dashboard_page_has_mobile_labeled_operational_rows():
    assert 'class="mobile-table"' in _BEGINNER_PAGE
    assert 'data-label="账户"' in _BEGINNER_PAGE
    assert 'data-label="状态"' in _BEGINNER_PAGE
    assert "table.mobile-table thead" in _BEGINNER_PAGE


def test_dashboard_page_collapses_long_ai_evidence_by_default():
    assert "function expandableEvidence(value)" in _BEGINNER_PAGE
    assert "evidence-details" in _BEGINNER_PAGE
    assert "slice().reverse().slice(0,6)" in _BEGINNER_PAGE


def test_dashboard_page_collapses_completed_order_history_by_default():
    assert '<details class="history-details">' in _BEGINNER_PAGE
    assert '<details class="history-details" open>' not in _BEGINNER_PAGE


def test_dashboard_page_keeps_unknown_service_status_neutral():
    assert "function serviceStatusKind(status)" in _BEGINNER_PAGE
    assert 'if(status==="unknown")return ""' in _BEGINNER_PAGE


def test_dashboard_page_does_not_expose_decorative_arrows_to_accessibility_tree():
    assert 'content:"→"' not in _BEGINNER_PAGE
    assert 'content:"↓"' not in _BEGINNER_PAGE


def test_dashboard_scheduler_without_recent_jobs_is_not_marked_healthy():
    assert "if(!jobValues.length)" in _BEGINNER_PAGE
    assert 'add("Scheduler","无最近状态"' in _BEGINNER_PAGE


def test_dashboard_escapes_allocator_quantity_before_inserting_html():
    assert '${esc(selected.quantity??"—")}' in _BEGINNER_PAGE


def test_dashboard_labels_quote_freshness_separately_from_heartbeat():
    assert '<span class="runtime-label">股票报价</span>' in _BEGINNER_PAGE
    assert 'const equityQuote=(state.market_data||{}).equity||{}' in _BEGINNER_PAGE
    assert "function ageLabel(seconds)" in _BEGINNER_PAGE


def test_dashboard_does_not_truncate_open_orders_in_portfolio_table():
    assert 'orderTable(openOrders,"当前没有等待成交的订单。",null)' in _BEGINNER_PAGE
    assert "function isUnfinishedOrder(status)" in _BEGINNER_PAGE


def test_dashboard_uses_total_sleeve_pnl_and_truthful_shadow_labels():
    assert "function metricTotalPnl(metrics)" in _BEGINNER_PAGE
    assert "function metricEntryCount(metrics)" in _BEGINNER_PAGE
    assert "function evidenceConclusion(evidence)" in _BEGINNER_PAGE
    assert '"影子研究 / 管理旧仓"' in _BEGINNER_PAGE
    assert 'closed:"—"' in _BEGINNER_PAGE
    assert '有效结果标签' in _BEGINNER_PAGE
    assert "样本数量已达标，但结果未通过盈利门槛" in _BEGINNER_PAGE
    assert "评估结论" in _BEGINNER_PAGE
    assert 'add("Broker 写入边界",safe?' in _BEGINNER_PAGE


def test_dashboard_ignores_client_disconnect_during_response(paper_root):
    class DisconnectedWriter:
        def write(self, _body):
            raise ConnectionAbortedError("browser closed")

    handler_type = make_handler(paper_root)
    handler = object.__new__(handler_type)
    handler.path = "/"
    handler.wfile = DisconnectedWriter()
    handler.send_response = lambda *_args, **_kwargs: None
    handler.send_header = lambda *_args, **_kwargs: None
    handler.end_headers = lambda: None

    handler.do_GET()


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


def test_dashboard_uses_latest_forward_exchange_session_over_stale_counters(
    paper_root,
):
    (paper_root / "state" / "daily_counters.json").write_text(
        json.dumps({"date": "2026-07-29", "trades": 0}),
        encoding="utf-8",
    )
    (paper_root / "state" / "runtime_heartbeat.json").write_text(
        json.dumps(
            {
                "last_heartbeat_at": "2026-07-30T15:00:00+00:00",
                "status": "ok",
                "payload": {
                    "latest_jobs": {
                        "forward": {
                            "status": "completed",
                            "output": {
                                "clock": {
                                    "session": "2026-07-30",
                                    "market_session": "regular",
                                }
                            },
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    (paper_root / "logs" / "llm_usage.jsonl").write_text(
        json.dumps(
            {
                "ts": "2026-07-30T14:30:00+00:00",
                "agent_name": "decision_manager",
                "estimated_cost_usd": 0.001,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    summary = build_dashboard_state(paper_root)["beginner_summary"]

    assert summary["session_date"] == "2026-07-30"
    assert summary["operations"]["llm_calls"] == 1


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


def test_dashboard_ignores_null_order_and_position_records(paper_root):
    for name in (
        "paper_orders.json",
        "paper_positions.json",
        "paper_option_orders.json",
        "paper_option_positions.json",
    ):
        (paper_root / "state" / name).write_text(
            json.dumps({"corrupt": None}),
            encoding="utf-8",
        )

    state = build_dashboard_state(paper_root)

    assert state["orders"] == []
    assert state["positions"] == []
    assert state["option_orders"] == []
    assert state["option_positions"] == []


def test_dashboard_treats_unknown_order_status_as_unfinished(paper_root):
    (paper_root / "state" / "paper_orders.json").write_text(
        json.dumps(
            {
                "unknown": {
                    "order_id": "unknown",
                    "symbol": "AAPL",
                    "status": "unexpected_state",
                    "created_at": "2026-07-04T14:00:00+00:00",
                }
            }
        ),
        encoding="utf-8",
    )

    state = build_dashboard_state(paper_root)

    assert state["orders"][0]["order_id"] == "unknown"
    assert state["order_history_summary"] == {
        "open_total": 1,
        "completed_total": 0,
        "completed_included": 0,
    }


def test_dashboard_requires_live_readonly_to_be_disabled_in_paper_mode(
    paper_root,
):
    config_path = paper_root / "config" / "paper_mode.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["mode"] = {
        "paper": True,
        "live_readonly": True,
        "live_trading": False,
    }
    config_path.write_text(
        yaml.safe_dump(config, sort_keys=False),
        encoding="utf-8",
    )

    state = build_dashboard_state(paper_root)

    assert state["mode"] == {
        "paper": True,
        "live_readonly": True,
        "live_trading": False,
    }


def test_dashboard_reports_equity_and_option_quote_times_independently(
    paper_root,
    monkeypatch,
):
    monkeypatch.setattr(
        dashboard,
        "utc_now",
        lambda: "2026-07-04T14:00:30+00:00",
    )
    decision = {
        "event": "baseline_decision",
        "ts": "2026-07-04T14:00:11+00:00",
        "decision": {
            "snapshot_id": "freshness-1",
            "ticker": "AAPL",
            "action": "no_trade",
            "regime": {"eligible": True},
            "technical": {},
        },
        "snapshot": {
            "market_session": "regular",
            "data_cutoff_time": "2026-07-04T14:00:10+00:00",
            "market_data": {
                "quote": {
                    "symbol": "AAPL",
                    "asof": "2026-07-04T14:00:10+00:00",
                    "source": "alpaca:iex",
                }
            },
        },
    }
    (paper_root / "logs" / "decisions.jsonl").write_text(
        json.dumps(decision) + "\n",
        encoding="utf-8",
    )
    option_diagnostic = {
        "ts": "2026-07-04T14:00:21+00:00",
        "diagnostics": {
            "quotes_observed_at": "2026-07-04T14:00:20+00:00",
        },
    }
    (paper_root / "logs" / "option_selection_diagnostics.jsonl").write_text(
        json.dumps(option_diagnostic) + "\n",
        encoding="utf-8",
    )

    market_data = build_dashboard_state(paper_root)["market_data"]

    assert market_data["equity"] == {
        "observed_at": "2026-07-04T14:00:10+00:00",
        "age_seconds": 20.0,
        "stale": False,
        "source": "alpaca:iex",
    }
    assert market_data["options"] == {
        "observed_at": "2026-07-04T14:00:20+00:00",
        "age_seconds": 10.0,
        "stale": False,
        "source": None,
    }


def test_dashboard_marks_future_quote_timestamp_stale(paper_root, monkeypatch):
    monkeypatch.setattr(
        dashboard,
        "utc_now",
        lambda: "2026-07-04T14:00:30+00:00",
    )
    decision = {
        "event": "baseline_decision",
        "snapshot": {
            "market_data": {
                "quote": {
                    "asof": "2026-07-04T14:00:31+00:00",
                    "source": "alpaca:iex",
                }
            }
        },
    }
    (paper_root / "logs" / "decisions.jsonl").write_text(
        json.dumps(decision) + "\n",
        encoding="utf-8",
    )

    observation = build_dashboard_state(paper_root)["market_data"]["equity"]

    assert observation["age_seconds"] == 0.0
    assert observation["stale"] is True


def test_dashboard_fails_closed_for_invalid_or_future_heartbeat_time(
    paper_root,
    monkeypatch,
):
    monkeypatch.setattr(
        dashboard,
        "utc_now",
        lambda: "2026-07-04T14:00:30+00:00",
    )
    heartbeat_path = paper_root / "state" / "runtime_heartbeat.json"
    heartbeat_path.write_text(
        json.dumps({"last_heartbeat_at": "not-a-time", "status": "ok"}),
        encoding="utf-8",
    )

    invalid = build_dashboard_state(paper_root)["heartbeat"]

    assert invalid["age_seconds"] is None
    assert invalid["stale"] is True
    assert invalid["effective_status"] == "stale"

    heartbeat_path.write_text(
        json.dumps(
            {
                "last_heartbeat_at": "2026-07-04T14:00:31+00:00",
                "status": "ok",
            }
        ),
        encoding="utf-8",
    )

    future = build_dashboard_state(paper_root)["heartbeat"]

    assert future["age_seconds"] == 0.0
    assert future["stale"] is True
    assert future["effective_status"] == "stale"


def test_dashboard_keeps_recent_completed_orders_after_open_order_selection(
    paper_root,
):
    orders = {}
    for index in range(35):
        order_id = f"open-{index:02d}"
        orders[order_id] = {
            "order_id": order_id,
            "symbol": "AAPL",
            "status": "open",
            "created_at": f"2026-07-04T14:{index:02d}:00+00:00",
        }
    orders["filled-old"] = {
        "order_id": "filled-old",
        "symbol": "MSFT",
        "status": "filled",
        "created_at": "2026-07-04T13:00:00+00:00",
    }
    (paper_root / "state" / "paper_orders.json").write_text(
        json.dumps(orders),
        encoding="utf-8",
    )

    state = build_dashboard_state(paper_root)

    assert any(order.get("order_id") == "filled-old" for order in state["orders"])
    assert state["order_history_summary"] == {
        "open_total": 35,
        "completed_total": 1,
        "completed_included": 1,
    }


def test_dashboard_metrics_cache_invalidates_when_state_changes(
    paper_root,
    monkeypatch,
):
    calls = []

    def fake_metrics(root, namespace=None):
        calls.append((str(root), namespace))
        return {"call_count": len(calls)}

    monkeypatch.setattr(dashboard, "calculate_metrics", fake_metrics)
    dashboard._cached_metrics.cache_clear()

    first = dashboard._safe_metrics(paper_root)
    second = dashboard._safe_metrics(paper_root)
    account_path = paper_root / "state" / "paper_account.json"
    account = json.loads(account_path.read_text(encoding="utf-8"))
    account["cache_marker"] = "changed"
    account_path.write_text(json.dumps(account), encoding="utf-8")
    third = dashboard._safe_metrics(paper_root)

    assert first == second == {"call_count": 1}
    assert third == {"call_count": 2}
    assert len(calls) == 2


def test_dashboard_ai_metrics_cache_invalidates_when_decision_log_changes(
    paper_root,
    monkeypatch,
):
    calls = []

    def fake_metrics(root, namespace=None):
        calls.append((str(root), namespace))
        return {"call_count": len(calls)}

    monkeypatch.setattr(dashboard, "calculate_metrics", fake_metrics)
    dashboard._cached_metrics.cache_clear()

    first = dashboard._safe_metrics(
        paper_root,
        namespace="ai_gated_technical_v1",
    )
    second = dashboard._safe_metrics(
        paper_root,
        namespace="ai_gated_technical_v1",
    )
    (paper_root / "logs" / "ai_gated_decisions.jsonl").write_text(
        '{"event":"decision"}\n',
        encoding="utf-8",
    )
    third = dashboard._safe_metrics(
        paper_root,
        namespace="ai_gated_technical_v1",
    )

    assert first == second == {"call_count": 1}
    assert third == {"call_count": 2}
    assert len(calls) == 2


def test_dashboard_degrades_only_news_drift_metrics_on_database_error(
    paper_root,
    monkeypatch,
):
    def fail_metrics(_root):
        raise sqlite3.DatabaseError("corrupt database")

    monkeypatch.setattr(dashboard, "calculate_news_drift_metrics", fail_metrics)

    state = build_dashboard_state(paper_root)

    assert state["news_drift"]["metrics"] == {
        "strategy": "llm_news_drift_v1",
        "metrics_available": False,
        "error": "metrics unavailable: DatabaseError",
    }


def test_dashboard_news_drift_cache_invalidates_on_sqlite_wal_change(
    paper_root,
    monkeypatch,
):
    calls = []

    def fake_metrics(_root):
        calls.append(len(calls) + 1)
        return {"call_count": calls[-1]}

    monkeypatch.setattr(dashboard, "calculate_news_drift_metrics", fake_metrics)
    dashboard._cached_news_drift_metrics.cache_clear()

    first = dashboard._safe_news_drift_metrics(paper_root)
    second = dashboard._safe_news_drift_metrics(paper_root)
    (paper_root / "state" / "news_events.sqlite-wal").write_bytes(b"wal-update")
    third = dashboard._safe_news_drift_metrics(paper_root)

    assert first == second == {"metrics_available": True, "call_count": 1}
    assert third == {"metrics_available": True, "call_count": 2}
    assert calls == [1, 2]


def test_dashboard_news_drift_cache_invalidates_on_usage_log_change(
    paper_root,
    monkeypatch,
):
    calls = []

    def fake_metrics(_root):
        calls.append(len(calls) + 1)
        return {"call_count": calls[-1]}

    monkeypatch.setattr(dashboard, "calculate_news_drift_metrics", fake_metrics)
    dashboard._cached_news_drift_metrics.cache_clear()

    first = dashboard._safe_news_drift_metrics(paper_root)
    second = dashboard._safe_news_drift_metrics(paper_root)
    (paper_root / "logs" / "llm_usage.jsonl").write_text(
        '{"agent_name":"news_drift_headline_agent"}\n',
        encoding="utf-8",
    )
    third = dashboard._safe_news_drift_metrics(paper_root)

    assert first == second == {"metrics_available": True, "call_count": 1}
    assert third == {"metrics_available": True, "call_count": 2}
    assert calls == [1, 2]


def test_dashboard_jsonl_tail_handles_large_utf8_crlf_records(tmp_path):
    path = tmp_path / "large-lines.jsonl"
    records = [
        {"index": 1, "payload": "旧" * 40000},
        {"index": 2, "payload": "中" * 40000},
        {"index": 3, "payload": "新" * 40000},
    ]
    path.write_bytes(
        b"\r\n".join(
            json.dumps(record, ensure_ascii=False).encode("utf-8")
            for record in records
        )
        + b'\r\n{"index":4'
    )

    assert _read_jsonl(path, limit=2) == records[-2:]


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


def test_trade_funnel_finds_allocator_proposal_bottleneck() -> None:
    now = "2026-08-21T20:00:00+00:00"
    audit_records = [
        {
            "ts": "2026-08-21T18:00:00+00:00",
            "event": "forward_cycle_complete",
            "snapshots": 25,
            "active_candidates": 4,
            "orders": [],
            "option_decisions": [
                {
                    "action": "buy_to_open",
                    "execution_status": "entry_frozen",
                }
            ],
            "option_entries": [],
        },
        {
            "ts": "2026-08-18T18:00:00+00:00",
            "event": "forward_cycle_complete",
            "snapshots": 999,
            "active_candidates": 999,
        },
    ]
    allocator_cycles = [
        {
            "ts": "2026-08-21T18:05:00+00:00",
            "event": "ai_instrument_allocator_stage_complete",
            "stage": "intraday",
            "skipped": [
                {
                    "ticker": "A",
                    "reason": "structured model failure: actionable signal holding period does not match horizon",
                },
                {
                    "ticker": "B",
                    "reason": "Model cited unsupported evidence.",
                },
                {
                    "ticker": "C",
                    "reason": "Challenge veto recommended no_trade.",
                },
                {
                    "ticker": "D",
                    "reason": "ticker cooldown active and no new event",
                },
            ],
            "plans": [{"plan_id": "plan-e"}],
            "executions": [
                {
                    "status": "no_trade",
                    "reason": "fresh executable data failed closed: quote unavailable",
                }
            ],
            "paper_orders_created": 0,
        }
    ]
    allocator_decisions = [
        {
            "ts": "2026-08-21T18:01:00+00:00",
            "challenge": {
                "hard_veto": True,
                "hard_veto_reasons": [
                    {"code": "critical_fact_conflict", "detail": "Conflicting filing."}
                ],
                "soft_concerns": [],
            },
            "signal": {"action": "no_trade"},
        },
        {
            "ts": "2026-08-21T18:02:00+00:00",
            "signal": {
                "action": "propose_trade",
                "horizon": "next_close",
                "probability_status": "uncalibrated",
                "signed_return_probability_buckets": {
                    "return_lt_minus_5_pct": 0.05,
                    "return_minus_5_to_minus_2_pct": 0.10,
                    "return_minus_2_to_minus_0_5_pct": 0.15,
                    "return_minus_0_5_to_plus_0_5_pct": 0.20,
                    "return_plus_0_5_to_plus_2_pct": 0.25,
                    "return_plus_2_to_plus_5_pct": 0.15,
                    "return_gt_plus_5_pct": 0.10,
                },
            },
        },
        {
            "ts": "2026-08-21T18:03:00+00:00",
            "challenge": {
                "hard_veto": False,
                "hard_veto_reasons": [],
                "soft_concerns": [
                    {"code": "partial_price_in", "detail": "Partly priced in."}
                ],
            },
            "signal": {
                "action": "watch",
                "watch_reason": "Direction exists but thesis is incomplete.",
                "horizon": "next_close",
                "probability_status": "uncalibrated",
                "signed_return_probability_buckets": {
                    "return_lt_minus_5_pct": 0.02,
                    "return_minus_5_to_minus_2_pct": 0.03,
                    "return_minus_2_to_minus_0_5_pct": 0.05,
                    "return_minus_0_5_to_plus_0_5_pct": 0.60,
                    "return_plus_0_5_to_plus_2_pct": 0.10,
                    "return_plus_2_to_plus_5_pct": 0.10,
                    "return_gt_plus_5_pct": 0.10,
                },
            },
        },
    ]

    funnel = _build_trade_funnel(
        now=now,
        allocator_profile={
            "minimum_direction_mass": 0.50,
            "minimum_direction_margin": 0.15,
        },
        audit_records=audit_records,
        allocator_cycles=allocator_cycles,
        allocator_decisions=allocator_decisions,
        allocator_fill_records=[],
    )

    assert funnel["window_hours"] == 48
    assert funnel["allocator"]["candidate_reviews"] == 5
    assert funnel["allocator"]["ranking_inputs"] == 3
    assert funnel["allocator"]["deep_research"] == 3
    assert funnel["allocator"]["model_decisions"] == 3
    assert funnel["allocator"]["watch"] == 1
    assert funnel["allocator"]["trade_proposals"] == 1
    assert funnel["allocator"]["hard_veto"] == 1
    assert funnel["allocator"]["soft_concern"] == 1
    assert funnel["allocator"]["direction_threshold_passes"] == 1
    assert funnel["allocator"]["execution_attempts"] == 1
    assert funnel["allocator"]["paper_orders"] == 0
    assert funnel["allocator"]["paper_fills"] == 0
    assert funnel["baselines"]["equity"]["signals"] == 4
    assert funnel["baselines"]["options"]["signals"] == 1
    assert funnel["baselines"]["equity"]["executable"] is False
    assert funnel["root_cause"]["code"] == "allocator_proposal_bottleneck"
    assert funnel["blockers"][0]["count"] >= 1


def test_trade_funnel_uses_frozen_allocator_replay_for_point_in_time_counts() -> None:
    replay = {
        "window_hours": 48,
        "window_started_at": "2026-08-24T07:16:12+00:00",
        "asof": "2026-08-26T07:16:12+00:00",
        "old_policy": {
            "candidates": 0,
            "ranking_input": 0,
            "deep_research": 0,
            "structured_decisions": 0,
            "watch": 0,
            "proposals": 0,
            "allocations": 0,
            "selected_instruments": 0,
            "paper_orders": 0,
            "paper_fills": 0,
        },
        "new_policy": {
            "candidates": 0,
            "ranking_input": 0,
            "deep_research": 0,
            "structured_decisions": 0,
            "watch": 0,
            "proposals": 0,
            "allocations": 0,
            "selected_instruments": 0,
            "paper_orders": 0,
            "paper_fills": 0,
        },
        "observed_audit_funnel": {
            "candidates": 56,
            "ranking_input": 40,
            "deep_research": 20,
            "structured_decisions": 22,
            "watch": 0,
            "proposals": 2,
            "allocations": 1,
            "selected_instruments": 0,
            "paper_orders": 0,
            "paper_fills": 0,
        },
        "comparison": {
            "proposal_delta": 0,
            "watch_delta": 0,
            "estimated_avoidable_rank_only_cooldowns": 11,
            "candidate_linkage_complete": False,
            "legacy_ambiguous_veto_count": 17,
            "explanation": "Strict replay does not synthesize model decisions.",
        },
        "blockers": {
            "cooldown": 16,
            "hard_veto": 17,
            "soft_concern": 3,
            "model_no_trade": 3,
            "direction_gate": 1,
            "option_affordability": 6,
            "spread_liquidity": 20,
        },
        "point_in_time": {
            "violation_count": 171,
            "replayable_snapshot_count": 0,
            "excluded_snapshot_count": 58,
        },
        "snapshot_integrity": {"checked": 58, "valid": 58, "invalid": 0},
        "historical_orders_created": 0,
        "live_order_tools_called": False,
    }

    funnel = _build_trade_funnel(
        now="2026-08-26T08:00:00+00:00",
        allocator_profile={
            "minimum_direction_mass": 0.50,
            "minimum_direction_margin": 0.15,
        },
        audit_records=[],
        allocator_cycles=[],
        allocator_decisions=[],
        allocator_fill_records=[],
        allocator_policy_replay=replay,
    )

    assert funnel["asof"] == replay["asof"]
    assert funnel["allocator"]["candidate_reviews"] == 56
    assert funnel["allocator"]["ranking_inputs"] == 40
    assert funnel["allocator"]["model_decisions"] == 22
    assert funnel["allocator"]["trade_proposals"] == 2
    assert funnel["allocator"]["selected_instruments"] == 0
    assert funnel["replay_comparison"]["new_policy"]["ranking_input"] == 0
    assert funnel["replay_comparison"]["observed_audit_funnel"]["ranking_input"] == 40
    assert funnel["replay_comparison"]["comparison"]["proposal_delta"] == 0
    assert funnel["replay_comparison"]["historical_orders_created"] == 0
    assert funnel["blockers"][0]["code"] == "spread_liquidity"


def test_dashboard_state_loads_allocator_policy_replay_artifact(paper_root) -> None:
    replay_path = (
        paper_root
        / "state"
        / "strategy_sleeves"
        / "ai_instrument_allocator_v1"
        / "allocator_policy_replay_latest.json"
    )
    replay_path.parent.mkdir(parents=True, exist_ok=True)
    replay_path.write_text(
        json.dumps(
            {
                "window_hours": 48,
                "window_started_at": "2026-08-24T07:16:12+00:00",
                "asof": "2026-08-26T07:16:12+00:00",
                "old_policy": {
                    "candidates": 0,
                    "ranking_input": 0,
                    "deep_research": 0,
                    "structured_decisions": 0,
                    "watch": 0,
                    "proposals": 0,
                    "allocations": 0,
                    "selected_instruments": 0,
                    "paper_orders": 0,
                    "paper_fills": 0,
                },
                "new_policy": {
                    "candidates": 0,
                    "ranking_input": 0,
                    "deep_research": 0,
                    "structured_decisions": 0,
                    "watch": 0,
                    "proposals": 0,
                    "allocations": 0,
                    "selected_instruments": 0,
                    "paper_orders": 0,
                    "paper_fills": 0,
                },
                "observed_audit_funnel": {
                    "candidates": 56,
                    "ranking_input": 40,
                    "deep_research": 20,
                    "structured_decisions": 22,
                    "watch": 0,
                    "proposals": 2,
                    "allocations": 1,
                    "selected_instruments": 0,
                    "paper_orders": 0,
                    "paper_fills": 0,
                },
                "comparison": {"proposal_delta": 0},
                "blockers": {"cooldown": 16},
                "snapshot_integrity": {"checked": 58, "valid": 58, "invalid": 0},
                "point_in_time": {
                    "violation_count": 171,
                    "replayable_snapshot_count": 0,
                    "excluded_snapshot_count": 58,
                },
                "historical_orders_created": 0,
                "live_order_tools_called": False,
            }
        ),
        encoding="utf-8",
    )

    state = build_dashboard_state(paper_root)

    assert state["trade_funnel"]["asof"] == "2026-08-26T07:16:12+00:00"
    assert state["trade_funnel"]["allocator"]["candidate_reviews"] == 56
    assert (
        state["trade_funnel"]["replay_comparison"]["observed_audit_funnel"]["ranking_input"]
        == 40
    )


def test_beginner_dashboard_contains_rolling_opportunity_funnel() -> None:
    assert "过去 48 小时观测审计漏斗" in _BEGINNER_PAGE
    assert "不是严格 point-in-time 回放" in _BEGINNER_PAGE
    assert "不代表订单" in _BEGINNER_PAGE
    assert "观察 Watch" in _BEGINNER_PAGE
    assert "旧规则 → 新规则" in _BEGINNER_PAGE
    assert "估计" in _BEGINNER_PAGE


def test_dashboard_separates_functional_historical_and_forward_evidence(
    paper_root: Path,
) -> None:
    report_path = paper_root / "reports" / "allocator_validation_latest.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    schema_dir = paper_root / "schemas"
    schema_dir.mkdir(parents=True, exist_ok=True)
    source_schema = (
        Path(__file__).resolve().parents[1]
        / "schemas"
        / "allocator_historical_validation_report.schema.json"
    )
    (schema_dir / source_schema.name).write_bytes(source_schema.read_bytes())
    report_path.write_text(
        json.dumps(
            {
                "schema_version": "allocator-historical-validation-v1",
                "strategy": "ai_instrument_allocator_v1",
                "generated_at": "2026-08-26T20:00:00+00:00",
                    "functional_liveness": {
                        "status": "passed",
                        "summary": {"passed": 3, "scenario_count": 3},
                        "scenarios": [
                            {"scenario_id": value}
                            for value in (
                                "bullish_equity",
                                "bullish_call",
                                "bearish_put",
                            )
                        ],
                        "historical_performance_claimed": False,
                    "forward_performance_claimed": False,
                },
                "historical_performance": {
                    "evidence_type": "strict_historical_diagnostic",
                    "historical_performance_available": False,
                    "strict_funnel": {
                        "counts": {
                            "candidates": 12,
                            "proposals": 2,
                            "paper_fills": 1,
                        },
                        "conversion_rates": {},
                        "outcome_rates": {},
                    },
                    "time_validation": {
                        "time_violation_count": 0,
                        "source_violation_count": 0,
                        "admitted_violation_count": 0,
                    },
                    "data_completeness": {
                        "equity": {},
                        "options": {"executable_pnl_claim_allowed": False},
                        "allowed_claims": ["synthetic_option_sensitivity"],
                    },
                    "manifest": {
                        "strategy_version": "test",
                        "prompt_version": "test",
                        "schema_version": "allocator-historical-validation-v1",
                        "config_hashes": {},
                        "model_id": "test",
                        "data_cutoff": "2026-08-26T20:00:00+00:00",
                        "manifest_hash": "test",
                    },
                    "llm_replay": {
                        "mode": "recorded_outputs_only",
                        "strategy_reexecution_performed": False,
                        "diagnostic_only": True,
                        "current_model_profitability_proof": False,
                    },
                    "historical_orders_created_by_replay": 0,
                    "live_broker_write_calls": 0,
                    "live_order_tools_called": False,
                },
                "forward_evidence": {
                    "evidence_type": "forward_paper_evidence",
                    "source": "existing isolated paper sleeve",
                    "profitability_claim": "insufficient_forward_evidence",
                    "closed_trade_count": 4,
                    "realized_pnl_usd": -12.5,
                },
                "walk_forward_readiness": {
                    "status": "blocked",
                    "supported_modes": ["expanding", "rolling"],
                    "partition_contract_requires_separation": True,
                    "partition_contract_requires_matured_labels": True,
                    "development_calibration_holdout_separated": False,
                    "matured_labels_only": False,
                    "labeled_dataset_provided": False,
                    "labeled_record_count": 0,
                    "horizons": [],
                    "actual_partitions_run": False,
                    "leakage_checks_run": False,
                    "equity_executable_backtest_ready": False,
                    "option_executable_pnl_ready": False,
                    "synthetic_option_sensitivity_allowed": True,
                    "blockers": ["missing_point_in_time_labeled_outcome_dataset"],
                    "limitations": ["incomplete_historical_option_chain"],
                    "claim_boundary": "No historical profitability claim is allowed.",
                },
                "evidence_boundaries": {
                    "functional_liveness": "liveness only",
                    "historical_performance": "diagnostic only",
                    "forward_evidence": "forward only",
                },
                "acceptance": {
                    "time_violation_count": 0,
                    "admitted_time_violation_count": 0,
                    "historical_orders_created": 0,
                    "live_broker_write_calls": 0,
                    "functional_source_root_unchanged": True,
                    "forward_state_logs_unchanged": True,
                    "forward_protected_file_count": 0,
                    "forward_protected_scope": (
                        "all files under allocator state, allocator logs, and immutable snapshots"
                    ),
                },
            }
        ),
        encoding="utf-8",
    )

    validation = build_dashboard_state(paper_root)["allocator_validation"]

    assert [line["key"] for line in validation["evidence_lines"]] == [
        "functional_liveness",
        "historical_performance",
        "forward_evidence",
    ]
    assert validation["evidence_lines"][0]["status"] == "passed"
    assert validation["evidence_lines"][1]["status"] == "diagnostic_only"
    assert validation["evidence_lines"][2]["status"] == "insufficient_forward_evidence"
    assert validation["strict_funnel"]["candidates"] == 12
    assert validation["option_claim"] == "synthetic_option_sensitivity_only"
    assert validation["walk_forward_readiness"]["status"] == "blocked"


def test_dashboard_never_executes_allocator_policy_replay_on_request(
    paper_root,
    monkeypatch,
) -> None:
    import scripts.replay.allocator_policy_replay as replay_module

    monkeypatch.setattr(
        replay_module,
        "run_allocator_policy_replay",
        lambda *_args, **_kwargs: pytest.fail("dashboard must not execute replay"),
    )

    state = build_dashboard_state(paper_root)

    assert "trade_funnel" in state


def test_beginner_dashboard_explains_validation_evidence_boundaries() -> None:
    assert "三种证据不要混淆" in _BEGINNER_PAGE
    assert "功能能跑通、历史数据表现、真实向前模拟是三件不同的事" in _BEGINNER_PAGE
    assert "synthetic option sensitivity" in _BEGINNER_PAGE
    assert "不能把它当成期权历史 PnL" in _BEGINNER_PAGE
    assert "保守情景估计（非成交 PnL）" in _BEGINNER_PAGE
    assert "Walk-forward 收益验证尚未开始" in _BEGINNER_PAGE
    assert "缺少带成熟结果标签的历史样本" in _BEGINNER_PAGE
