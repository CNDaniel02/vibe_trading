from __future__ import annotations

from pathlib import Path

from scripts.core.config import assert_paper_mode, load_runtime_config
from scripts.adapters.alpaca_market_data_adapter import AlpacaMarketDataAdapter
from scripts.adapters.exa_news_adapter import ExaNewsAdapter
from scripts.adapters.vibe_runtime import VibeRuntime
from scripts.runtime.watchdog import check_runtime


def run_healthcheck(root: str | Path, require_heartbeat: bool = False) -> dict:
    root = Path(root)
    config = load_runtime_config(root)
    assert_paper_mode(config)
    state_files = ["paper_account.json", "paper_positions.json", "paper_orders.json", "daily_counters.json"]
    missing = [name for name in state_files if not (root / "state" / name).exists()]
    watchdog = check_runtime(root).to_dict() if require_heartbeat else {"healthy": True, "fail_closed": False, "reason": "heartbeat not required"}
    integrations = config.get("integrations", {})
    vibe = VibeRuntime(root, integrations.get("vibe", {})).status().to_dict()
    forward = integrations.get("forward_data", {})
    alpaca = AlpacaMarketDataAdapter(forward.get("alpaca", {})).readiness()
    exa = ExaNewsAdapter(forward.get("exa", {})).readiness()
    ok = not missing and watchdog["healthy"]
    return {
        "ok": ok,
        "paper_mode": True,
        "missing_state_files": missing,
        "watchdog": watchdog,
        "integrations": {"vibe": vibe, "alpaca": alpaca, "exa": exa},
        "forward_ready": bool(vibe["ready"] and alpaca["ready"]),
    }


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--require-heartbeat", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run_healthcheck(args.root, args.require_heartbeat), indent=2, sort_keys=True))
