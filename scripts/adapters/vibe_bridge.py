from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import time
from pathlib import Path
from typing import Any


FORBIDDEN_TOOL_FRAGMENTS = ("order", "trade", "position", "account", "broker", "cancel", "shell", "bash", "write_file")


def _load_payload() -> dict[str, Any]:
    raw = json.load(sys.stdin)
    if not isinstance(raw, dict):
        raise ValueError("bridge payload must be an object")
    return raw


def _configure_imports(vibe_repo: Path) -> None:
    agent_root = vibe_repo / "agent"
    if not agent_root.is_dir():
        raise ValueError("Vibe agent package not found")
    sys.path.insert(0, str(agent_root))


def _fetch_bars(payload: dict[str, Any]) -> dict[str, Any]:
    from backtest.loaders.registry import get_loader_cls_with_fallback

    source = str(payload.get("source", "yahoo"))
    codes = [str(item).upper() for item in payload.get("codes", [])]
    if not codes:
        raise ValueError("codes must not be empty")
    loader_cls = get_loader_cls_with_fallback(source)
    loader = loader_cls()
    frames = loader.fetch(
        codes,
        str(payload["start_date"]),
        str(payload["end_date"]),
        interval=str(payload.get("interval", "1D")),
    )
    output: dict[str, list[dict[str, Any]]] = {}
    for code, frame in frames.items():
        rows: list[dict[str, Any]] = []
        for index, row in frame.sort_index().iterrows():
            timestamp = index.isoformat() if hasattr(index, "isoformat") else str(index)
            rows.append(
                {
                    "timestamp": timestamp,
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": float(row.get("volume", 0.0)),
                }
            )
        output[str(code)] = rows
    return {"ok": True, "source_requested": source, "source_effective": getattr(loader, "name", source), "bars": output}


def _load_readonly_preset(payload: dict[str, Any]) -> tuple[dict[str, Any], set[str]]:
    import yaml

    preset_path = Path(str(payload["preset_file"])).resolve()
    data = yaml.safe_load(preset_path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("preset must contain a mapping")
    allowed = {str(item) for item in payload.get("allowed_tools", [])}
    for agent in data.get("agents", []):
        for tool in agent.get("tools", []):
            normalized = str(tool).lower()
            if tool not in allowed or any(fragment in normalized for fragment in FORBIDDEN_TOOL_FRAGMENTS):
                raise ValueError(f"unsafe or unapproved swarm tool: {tool}")
    return data, allowed


def _inspect_swarm(payload: dict[str, Any]) -> dict[str, Any]:
    data, allowed = _load_readonly_preset(payload)
    agent_ids = {str(agent["id"]) for agent in data.get("agents", [])}
    task_ids = {str(task["id"]) for task in data.get("tasks", [])}
    errors: list[str] = []
    for task in data.get("tasks", []):
        if str(task.get("agent_id")) not in agent_ids:
            errors.append(f"unknown agent for task {task.get('id')}")
        for dependency in task.get("depends_on", []):
            if str(dependency) not in task_ids:
                errors.append(f"unknown dependency {dependency}")
    return {
        "ok": not errors,
        "name": data.get("name"),
        "agents": len(agent_ids),
        "tasks": len(task_ids),
        "allowed_tools": sorted(allowed),
        "errors": errors,
    }


def _run_swarm(payload: dict[str, Any]) -> dict[str, Any]:
    data, _ = _load_readonly_preset(payload)
    from src.config import load_swarm_agent_config
    from src.swarm.models import RunStatus
    from src.swarm.runtime import SwarmRuntime
    from src.swarm.store import SwarmStore
    import src.swarm.presets as presets

    preset_file = Path(str(payload["preset_file"])).resolve()
    presets.PRESETS_DIR = preset_file.parent
    run_root = Path(str(payload["run_root"])).resolve()
    run_root.mkdir(parents=True, exist_ok=True)
    store = SwarmStore(base_dir=run_root)
    runtime = SwarmRuntime(store=store, agent_config=load_swarm_agent_config())
    run = runtime.start_run(str(data["name"]), dict(payload.get("variables", {})), include_shell_tools=False)
    timeout = float(payload.get("timeout_seconds", 1800))
    deadline = time.monotonic() + timeout
    current = run
    while time.monotonic() < deadline:
        loaded = store.load_run(run.id)
        if loaded is None:
            raise RuntimeError("Vibe swarm run record disappeared")
        current = loaded
        if current.status in (RunStatus.completed, RunStatus.failed, RunStatus.cancelled):
            break
        time.sleep(0.5)
    else:
        runtime.cancel_run(run.id)
        raise TimeoutError("Vibe swarm timed out")
    return {
        "ok": current.status == RunStatus.completed,
        "run_id": current.id,
        "status": current.status.value,
        "final_report": current.final_report,
        "input_tokens": current.total_input_tokens,
        "output_tokens": current.total_output_tokens,
        "tasks": [
            {"id": task.id, "agent_id": task.agent_id, "status": task.status.value, "error": task.error}
            for task in current.tasks
        ],
    }


def _run_backtest(payload: dict[str, Any]) -> dict[str, Any]:
    run_dir = Path(str(payload["run_dir"])).resolve()
    allowed_root = Path(str(payload["allowed_root"])).resolve()
    try:
        run_dir.relative_to(allowed_root)
    except ValueError as exc:
        raise ValueError("backtest run directory escapes the allowed root") from exc
    from src.tools import path_utils

    # Vibe's default run-root discovery imports the complete Swarm stack. The
    # adapter supplies a narrower root and avoids pulling live/MCP modules into
    # this research-only backtest process.
    path_utils._default_run_roots = lambda: [allowed_root]
    from backtest.runner import main as run_backtest

    captured_stdout = io.StringIO()
    captured_stderr = io.StringIO()
    with contextlib.redirect_stdout(captured_stdout), contextlib.redirect_stderr(captured_stderr):
        run_backtest(run_dir)
    metrics = run_dir / "artifacts" / "metrics.csv"
    if not metrics.is_file():
        raise RuntimeError("Vibe backtest did not produce metrics.csv")
    return {"ok": True, "run_dir": str(run_dir), "metrics_csv": str(metrics)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vibe-repo", required=True)
    parser.add_argument("--action", choices=["fetch_bars", "inspect_swarm", "run_swarm", "run_backtest"], required=True)
    args = parser.parse_args()
    try:
        _configure_imports(Path(args.vibe_repo).resolve())
        payload = _load_payload()
        handlers = {
            "fetch_bars": _fetch_bars,
            "inspect_swarm": _inspect_swarm,
            "run_swarm": _run_swarm,
            "run_backtest": _run_backtest,
        }
        result = handlers[args.action](payload)
    except Exception as exc:
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(result, sort_keys=True))
    if result.get("ok") is not True:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
