from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from scripts.core.audit import append_jsonl
from scripts.core.models import utc_now


@dataclass
class JobResult:
    job_name: str
    status: str
    started_at: str
    finished_at: str
    elapsed_seconds: float
    returncode: int | None
    timed_out: bool
    output: dict[str, Any] | None
    error: str | None
    resources: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SubprocessJobRunner:
    """Runs network-bound cycles behind hard deadlines and process-tree cleanup."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self._guard = threading.Lock()
        self._active: dict[
            str,
            tuple[subprocess.Popen[str], frozenset[str], str],
        ] = {}

    def run(
        self,
        job_name: str,
        module_args: list[str],
        *,
        timeout_seconds: float,
        mutates_state: bool,
        resources: set[str] | frozenset[str] | None = None,
    ) -> JobResult:
        started_at = utc_now()
        started = time.monotonic()
        required_resources = frozenset(
            resources if resources is not None else ({"global_state"} if mutates_state else set())
        )
        with self._guard:
            if job_name in self._active:
                return self._record_skipped(
                    self._skipped(job_name, started_at, "job already active", required_resources)
                )
            conflicts = sorted(
                name
                for name, item in self._active.items()
                if required_resources.intersection(item[1])
            )
            if conflicts:
                return self._record_skipped(
                    self._skipped(
                        job_name,
                        started_at,
                        f"resource conflict with active job(s): {', '.join(conflicts)}",
                        required_resources,
                    )
                )

            creationflags = 0
            popen_kwargs: dict[str, Any] = {}
            if os.name == "nt":
                creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                popen_kwargs["start_new_session"] = True
            command = [
                sys.executable,
                "-m",
                "scripts.orchestrator.forward_paper_service",
                "--root",
                str(self.root),
                *module_args,
            ]
            process = subprocess.Popen(
                command,
                cwd=self.root,
                env={**os.environ, "AUTO_TRADING_SUPERVISED_CHILD": "1"},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=creationflags,
                **popen_kwargs,
            )
            self._active[job_name] = (process, required_resources, started_at)

        timed_out = False
        stdout = ""
        stderr = ""
        try:
            stdout, stderr = process.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._terminate_process_tree(process)
            stdout, stderr = process.communicate(timeout=10)
        finally:
            with self._guard:
                self._active.pop(job_name, None)

        elapsed = time.monotonic() - started
        output = self._parse_last_json(stdout)
        if timed_out:
            status = "timed_out"
            error = f"hard deadline exceeded after {timeout_seconds:g}s"
        elif process.returncode == 0:
            status = "completed"
            error = None
        else:
            status = "failed"
            error = self._safe_error(stderr or stdout)
        result = JobResult(
            job_name=job_name,
            status=status,
            started_at=started_at,
            finished_at=utc_now(),
            elapsed_seconds=round(elapsed, 3),
            returncode=process.returncode,
            timed_out=timed_out,
            output=output,
            error=error,
            resources=sorted(required_resources),
        )
        append_jsonl(self.root, "runtime_jobs.jsonl", {"event": "runtime_job_finished", **result.to_dict()})
        return result

    def active_jobs(self) -> dict[str, dict[str, Any]]:
        with self._guard:
            return {
                name: {
                    "pid": process.pid,
                    "mutates_state": bool(resources),
                    "resources": sorted(resources),
                    "started_at": started_at,
                }
                for name, (process, resources, started_at) in self._active.items()
            }

    def terminate_all(self) -> None:
        with self._guard:
            processes = [item[0] for item in self._active.values()]
        for process in processes:
            self._terminate_process_tree(process)

    @staticmethod
    def _parse_last_json(stdout: str) -> dict[str, Any] | None:
        text = stdout.strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            pass
        for line in reversed(text.splitlines()):
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
        return None

    @staticmethod
    def _safe_error(text: str) -> str:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return (lines[-1] if lines else "child process failed")[:500]

    @staticmethod
    def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            return
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def _record_skipped(self, result: JobResult) -> JobResult:
        append_jsonl(
            self.root,
            "runtime_jobs.jsonl",
            {"event": "runtime_job_skipped", **result.to_dict()},
        )
        return result

    @staticmethod
    def _skipped(
        job_name: str,
        started_at: str,
        reason: str,
        resources: frozenset[str],
    ) -> JobResult:
        return JobResult(
            job_name=job_name,
            status="skipped",
            started_at=started_at,
            finished_at=utc_now(),
            elapsed_seconds=0.0,
            returncode=None,
            timed_out=False,
            output=None,
            error=reason,
            resources=sorted(resources),
        )
