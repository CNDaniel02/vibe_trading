from __future__ import annotations

import os
from pathlib import Path

from scripts.core.config import assert_paper_mode, load_runtime_config
from scripts.adapters.alpaca_market_data_adapter import AlpacaMarketDataAdapter
from scripts.adapters.exa_news_adapter import ExaNewsAdapter
from scripts.adapters.robinhood_mcp_market_data_adapter import RobinhoodMcpMarketDataAdapter
from scripts.adapters.robinhood_option_market_data_adapter import RobinhoodOptionMarketDataAdapter
from scripts.adapters.vibe_runtime import VibeRuntime
from scripts.runtime.watchdog import check_runtime


def run_healthcheck(root: str | Path, require_heartbeat: bool = False) -> dict:
    root = Path(root)
    config = load_runtime_config(root)
    assert_paper_mode(config)
    state_files = [
        "paper_account.json",
        "paper_positions.json",
        "paper_orders.json",
        "paper_option_positions.json",
        "paper_option_orders.json",
        "daily_counters.json",
    ]
    missing = [name for name in state_files if not (root / "state" / name).exists()]
    integrations = config.get("integrations", {})
    runtime = integrations.get("runtime", {})
    max_heartbeat_age_seconds = int(runtime.get("watchdog_max_age_seconds", 120))
    watchdog = (
        check_runtime(root, max_heartbeat_age_seconds=max_heartbeat_age_seconds).to_dict()
        if require_heartbeat
        else {"healthy": True, "fail_closed": False, "reason": "heartbeat not required"}
    )
    vibe = VibeRuntime(root, integrations.get("vibe", {})).status().to_dict()
    forward = integrations.get("forward_data", {})
    alpaca = AlpacaMarketDataAdapter(forward.get("alpaca", {})).readiness()
    robinhood_mcp = RobinhoodMcpMarketDataAdapter(integrations.get("robinhood_mcp", {}), root=root).readiness()
    option_data = RobinhoodOptionMarketDataAdapter(integrations.get("robinhood_mcp", {}), config, root).readiness()
    exa = ExaNewsAdapter(forward.get("exa", {})).readiness()
    llm_provider = str(config.get("llm", {}).get("provider", "mock"))
    llm_key_env = str(config.get("llm", {}).get("api", {}).get("api_key_env", "OPENAI_API_KEY"))
    llm_ready = llm_provider == "mock" or bool(os.getenv(llm_key_env))
    catalyst_enabled = bool(
        config.get("strategies", {})
        .get("exa_deepseek_catalyst_v1", {})
        .get("discovery", {})
        .get("enabled", False)
    )
    quote_provider = str(forward.get("quote_provider", "alpaca"))
    quote_data = {"alpaca": alpaca, "robinhood_mcp": robinhood_mcp}.get(quote_provider)
    if quote_data is None:
        quote_data = {"ready": False, "reason": f"unsupported quote provider: {quote_provider}"}
    ok = not missing and watchdog["healthy"]
    return {
        "ok": ok,
        "paper_mode": True,
        "missing_state_files": missing,
        "watchdog": watchdog,
        "integrations": {
            "vibe": vibe,
            "alpaca": alpaca,
            "robinhood_mcp": robinhood_mcp,
            "robinhood_option_data": option_data,
            "exa": exa,
        },
        "quote_provider": quote_provider,
        "quote_data": quote_data,
        "forward_ready": bool(vibe["ready"] and quote_data["ready"]),
        "catalyst_discovery": {
            "enabled": catalyst_enabled,
            "ready": bool(catalyst_enabled and robinhood_mcp["ready"] and exa["ready"] and llm_ready),
            "llm_provider": llm_provider,
            "llm_ready": llm_ready,
        },
    }


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--require-heartbeat", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run_healthcheck(args.root, args.require_heartbeat), indent=2, sort_keys=True))
