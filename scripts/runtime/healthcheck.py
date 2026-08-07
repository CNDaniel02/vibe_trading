from __future__ import annotations

import os
from pathlib import Path

from scripts.core.config import assert_paper_mode, load_runtime_config
from scripts.adapters.alpaca_market_data_adapter import AlpacaMarketDataAdapter
from scripts.adapters.exa_news_adapter import ExaNewsAdapter
from scripts.adapters.robinhood_mcp_market_data_adapter import RobinhoodMcpMarketDataAdapter
from scripts.adapters.robinhood_option_market_data_adapter import RobinhoodOptionMarketDataAdapter
from scripts.adapters.robinhood_discovery_adapter import RobinhoodDiscoveryAdapter
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
    discovery_config = {
        **config.get("strategies", {})
        .get("exa_deepseek_catalyst_v1", {})
        .get("discovery", {}),
        "excluded_symbols": config.get("universe", {}).get("excluded_symbols", []),
    }
    discovery_data = RobinhoodDiscoveryAdapter(
        integrations.get("robinhood_mcp", {}),
        discovery_config,
        root,
    ).readiness()
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
    ai_gated_enabled = bool(
        config.get("strategies", {})
        .get("ai_gated_technical_v1", {})
        .get("enabled", False)
    )
    news_drift_enabled = bool(
        config.get("strategies", {})
        .get("llm_news_drift_v1", {})
        .get("enabled", False)
    )
    options_enabled = bool(
        config.get("paper", {}).get("strategy_lines", {}).get("options", False)
        and config.get("options_universe", {}).get("enabled", False)
        and config.get("options_risk", {}).get("enabled", False)
    )
    quote_provider = str(forward.get("quote_provider", "alpaca"))
    fallback_quote_provider = str(forward.get("fallback_quote_provider", "")).strip() or None
    quote_data = {"alpaca": alpaca, "robinhood_mcp": robinhood_mcp}.get(quote_provider)
    if quote_data is None:
        quote_data = {"ready": False, "reason": f"unsupported quote provider: {quote_provider}"}
    fallback_quote_data = (
        {"alpaca": alpaca, "robinhood_mcp": robinhood_mcp}.get(fallback_quote_provider)
        if fallback_quote_provider
        else None
    )
    if fallback_quote_provider and fallback_quote_data is None:
        fallback_quote_data = {
            "ready": False,
            "reason": f"unsupported fallback quote provider: {fallback_quote_provider}",
        }
    quote_ready = bool(
        quote_data.get("ready")
        or (fallback_quote_data and fallback_quote_data.get("ready"))
    )
    runtime_healthy = not missing and watchdog["healthy"]
    forward_ready = bool(vibe["ready"] and quote_ready)
    catalyst_ready = bool(
        not catalyst_enabled
        or (discovery_data["ready"] and exa["ready"] and llm_ready)
    )
    ai_gated_ready = bool(
        not ai_gated_enabled
        or (discovery_data["ready"] and exa["ready"] and llm_ready)
    )
    news_drift_ready = bool(
        not news_drift_enabled
        or (discovery_data["ready"] and exa["ready"] and llm_ready)
    )
    options_ready = bool(not options_enabled or option_data["ready"])
    full_forward_evaluation_ready = bool(
        runtime_healthy
        and forward_ready
        and options_ready
        and catalyst_ready
        and ai_gated_ready
        and news_drift_ready
    )
    degraded_reasons: list[str] = []
    if quote_data is not None and not quote_data.get("ready") and quote_ready:
        degraded_reasons.append("primary quote provider unavailable; fallback is active")
    if options_enabled and not options_ready:
        degraded_reasons.append("enabled options line is not ready")
    if catalyst_enabled and not catalyst_ready:
        degraded_reasons.append("enabled catalyst discovery line is not ready")
    if ai_gated_enabled and not ai_gated_ready:
        degraded_reasons.append("enabled AI-gated paper line is not ready")
    if news_drift_enabled and not news_drift_ready:
        degraded_reasons.append("enabled news-drift shadow line is not ready")
    if not forward_ready:
        degraded_reasons.append("core forward equity data is not ready")
    if not runtime_healthy:
        operational_status = "failed"
    elif not full_forward_evaluation_ready:
        operational_status = "degraded"
    else:
        operational_status = "ok"
    return {
        "ok": full_forward_evaluation_ready,
        "runtime_healthy": runtime_healthy,
        "operational_status": operational_status,
        "degraded_reasons": degraded_reasons,
        "paper_mode": True,
        "missing_state_files": missing,
        "watchdog": watchdog,
        "integrations": {
            "vibe": vibe,
            "alpaca": alpaca,
            "robinhood_mcp": robinhood_mcp,
            "robinhood_option_data": option_data,
            "robinhood_discovery_data": discovery_data,
            "exa": exa,
        },
        "quote_provider": quote_provider,
        "quote_data": quote_data,
        "fallback_quote_provider": fallback_quote_provider,
        "fallback_quote_data": fallback_quote_data,
        "forward_ready": forward_ready,
        "options_line": {
            "enabled": options_enabled,
            "ready": options_ready,
        },
        "catalyst_discovery": {
            "enabled": catalyst_enabled,
            "ready": catalyst_ready,
            "llm_provider": llm_provider,
            "llm_ready": llm_ready,
        },
        "ai_gated_paper": {
            "enabled": ai_gated_enabled,
            "ready": ai_gated_ready,
            "llm_provider": llm_provider,
            "llm_ready": llm_ready,
        },
        "news_drift_shadow": {
            "enabled": news_drift_enabled,
            "ready": news_drift_ready,
            "execution": "shadow_only",
            "llm_provider": llm_provider,
            "llm_ready": llm_ready,
        },
        "full_forward_evaluation_ready": full_forward_evaluation_ready,
    }


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--require-heartbeat", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run_healthcheck(args.root, args.require_heartbeat), indent=2, sort_keys=True))
