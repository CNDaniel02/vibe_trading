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
    assert state["safety"]["allow_options"] is True
    assert state["safety"]["options_risk"]["allow_sell_to_open"] is False
    assert state["safety"]["options_risk"]["allow_margin"] is False
    assert state["safety"]["allow_fractional_shares"] is True
    assert state["candidates"][0]["ticker"] == "AAPL"
    assert "5-day price change is not positive" in state["candidates"][0]["reasons"]


def test_dashboard_handler_exposes_only_read_routes(paper_root):
    handler = make_handler(paper_root)
    assert handler.__name__ == "DashboardHandler"
    assert not hasattr(handler, "do_POST")
