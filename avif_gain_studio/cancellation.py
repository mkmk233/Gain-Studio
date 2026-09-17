from __future__ import annotations

import os
import subprocess
import threading
from typing import Protocol


class _Terminable(Protocol):
    def terminate(self) -> None: ...
    def kill(self) -> None: ...


class ConversionCancelled(RuntimeError):
    """Raised when the user stops the active conversion."""


class CancellationToken:
    """Thread-safe cancellation flag that also terminates active child processes."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._processes: set[_Terminable] = set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        if self.cancelled:
            raise ConversionCancelled("用户已停止任务。")

    def register_process(self, process: _Terminable) -> None:
        with self._lock:
            if self.cancelled:
                self._terminate_process(process)
                raise ConversionCancelled("用户已停止任务。")
            self._processes.add(process)

    def unregister_process(self, process: _Terminable) -> None:
        with self._lock:
            self._processes.discard(process)

    def cancel(self) -> None:
        self._event.set()
        with self._lock:
            processes = list(self._processes)
        for process in processes:
            self._terminate_process(process)

    @staticmethod
    def _terminate_process(process: _Terminable) -> None:
        try:
            pid = getattr(process, "pid", None)
            if os.name == "nt" and pid:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    check=False,
                )
                return
            process.terminate()
        except Exception:
            try:
                process.kill()
            except Exception:
                pass
