from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from scripts.adapters.errors import AdapterConfigurationError, AdapterDataError, AdapterSafetyError


@dataclass(frozen=True)
class VibeStatus:
    ready: bool
    repo_path: str
    expected_commit: str
    actual_commit: str | None
    clean_worktree: bool
    python_executable: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class VibeRuntime:
    """Pinned, subprocess-isolated boundary around the Vibe-Trading checkout."""

    def __init__(self, project_root: str | Path, config: dict[str, Any]) -> None:
        self.project_root = Path(project_root).resolve()
        self.config = config
        configured_repo = Path(str(config.get("repo_path", "")))
        self.repo_path = configured_repo.resolve() if configured_repo.is_absolute() else (self.project_root / configured_repo).resolve()
        python_env = str(config.get("python_executable_env", "VIBE_PYTHON_EXECUTABLE"))
        self.python_executable = os.getenv(python_env) or sys.executable
        self.timeout = float(config.get("subprocess_timeout_seconds", 120))

    def status(self) -> VibeStatus:
        expected = str(self.config.get("expected_commit", "")).strip()
        if not self.repo_path.is_dir():
            return VibeStatus(False, str(self.repo_path), expected, None, False, self.python_executable, "Vibe repo not found")
        try:
            actual = self._git("rev-parse", "HEAD").strip()
            clean = not bool(self._git("status", "--porcelain").strip())
        except (OSError, subprocess.SubprocessError):
            return VibeStatus(False, str(self.repo_path), expected, None, False, self.python_executable, "Vibe git status unavailable")
        if expected and actual != expected:
            return VibeStatus(False, str(self.repo_path), expected, actual, clean, self.python_executable, "Vibe commit does not match pinned version")
        if self.config.get("require_clean_worktree", True) and not clean:
            return VibeStatus(False, str(self.repo_path), expected, actual, clean, self.python_executable, "Vibe worktree is dirty")
        return VibeStatus(True, str(self.repo_path), expected, actual, clean, self.python_executable, "ready")

    def require_ready(self) -> VibeStatus:
        status = self.status()
        if not status.ready:
            raise AdapterConfigurationError(status.reason)
        return status

    def bridge(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        if action not in {"fetch_bars", "inspect_swarm", "run_swarm", "run_backtest"}:
            raise AdapterSafetyError(f"unsupported Vibe bridge action: {action}")
        self.require_ready()
        command = [
            self.python_executable,
            "-m",
            "scripts.adapters.vibe_bridge",
            "--vibe-repo",
            str(self.repo_path),
            "--action",
            action,
        ]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(self.project_root) + os.pathsep + env.get("PYTHONPATH", "")
        completed = subprocess.run(
            command,
            cwd=self.project_root,
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            timeout=self.timeout,
            env=env,
            check=False,
        )
        if completed.returncode != 0:
            raise AdapterDataError(f"Vibe bridge action {action} failed with exit code {completed.returncode}")
        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise AdapterDataError("Vibe bridge returned invalid JSON") from exc
        if not isinstance(result, dict) or result.get("ok") is not True:
            error = result.get("error", "unknown bridge error") if isinstance(result, dict) else "invalid bridge result"
            raise AdapterDataError(str(error))
        return result

    def _git(self, *args: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(self.repo_path), *args],
            text=True,
            capture_output=True,
            timeout=10,
            check=True,
        )
        return completed.stdout
