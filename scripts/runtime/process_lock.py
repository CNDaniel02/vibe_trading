from __future__ import annotations

import os
import sys
import ctypes
from pathlib import Path


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
            except FileExistsError:
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
        alive = self._process_is_alive(pid)
        return False if alive is not False else self._unlink_stale_lock()

    @staticmethod
    def _process_is_alive(pid: int) -> bool | None:
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
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def __enter__(self) -> "ProcessLock":
        if not self.acquire():
            raise RuntimeError(f"process lock already held: {self.path}")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()
