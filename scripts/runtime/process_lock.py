from __future__ import annotations

import os
import sys
import ctypes
import time
from pathlib import Path
from typing import Any


class ProcessLock:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.fd: int | None = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                self.fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except (FileExistsError, PermissionError):
                if not self._remove_stale_lock():
                    return False
        else:
            return False
        os.write(self.fd, str(os.getpid()).encode("ascii"))
        return True

    def _remove_stale_lock(self) -> bool:
        """Remove only a lock whose recorded owner process has exited."""
        try:
            pid = int(self.path.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            # An empty or malformed file can be a lock in the process of being
            # created. Fail closed rather than risking a duplicate runner.
            return False
        alive = self.process_is_alive(pid)
        return False if alive is not False else self._unlink_stale_lock()

    @staticmethod
    def process_is_alive(pid: int) -> bool | None:
        """Return None when liveness cannot be established safely."""
        if sys.platform == "win32":
            process_query_limited_information = 0x1000
            still_active = 259
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
            kernel32.OpenProcess.restype = ctypes.c_void_p
            kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
            kernel32.GetExitCodeProcess.restype = ctypes.c_int
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
            if not handle:
                # ERROR_INVALID_PARAMETER means there is no such PID. Access
                # denied is intentionally treated as unknown, not stale.
                return False if ctypes.get_last_error() == 87 else None
            try:
                exit_code = ctypes.c_ulong()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return None
                return exit_code.value == still_active
            finally:
                kernel32.CloseHandle(handle)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            return None
        return True

    @classmethod
    def inspect(cls, path: str | Path) -> dict[str, Any]:
        lock_path = Path(path)
        if not lock_path.exists():
            return {
                "present": False,
                "pid": None,
                "alive": False,
                "status": "missing",
            }
        try:
            pid = int(lock_path.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            return {
                "present": True,
                "pid": None,
                "alive": None,
                "status": "malformed",
            }
        alive = cls.process_is_alive(pid)
        return {
            "present": True,
            "pid": pid,
            "alive": alive,
            "status": "running" if alive is True else ("dead" if alive is False else "unknown"),
        }

    def _unlink_stale_lock(self) -> bool:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        return True

    def release(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        for _ in range(100):
            try:
                self.path.unlink()
                return
            except FileNotFoundError:
                return
            except PermissionError:
                time.sleep(0.005)
        raise PermissionError(f"could not release process lock: {self.path}")

    def __enter__(self) -> "ProcessLock":
        if not self.acquire():
            raise RuntimeError(f"process lock already held: {self.path}")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()
