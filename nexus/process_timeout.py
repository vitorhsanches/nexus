"""Wall-clock watchdog for long-running Codex subprocesses.

Manager and Worker subprocesses are drained with a blocking ``for line in
process.stdout`` loop, so a hung Codex process would hang the whole Run.
``ProcessWatchdog`` kills the process tree after a deadline; the blocked
stdout loop then sees EOF and the caller can report a timeout.
"""

from __future__ import annotations

import os
import subprocess
import threading


DEFAULT_WORKER_TIMEOUT_SECONDS = 60 * 60
DEFAULT_MANAGER_TIMEOUT_SECONDS = 30 * 60


def timeout_from_env(name: str, default: int) -> int | None:
    """Read a timeout in seconds from the environment.

    ``0`` or a negative value disables the watchdog; an invalid value falls
    back to ``default``.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else None


def kill_process_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        # PowerShell wrapper -> codex -> tool shells: kill the whole tree.
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            check=False,
        )
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass


class ProcessWatchdog:
    """Kill ``process`` if it is still running after ``timeout_seconds``."""

    def __init__(self, process: subprocess.Popen, timeout_seconds: int | None):
        self.process = process
        self.timeout_seconds = timeout_seconds
        self.fired = False
        self._timer: threading.Timer | None = None

    def _on_timeout(self) -> None:
        if self.process.poll() is None:
            self.fired = True
            kill_process_tree(self.process)

    def __enter__(self) -> "ProcessWatchdog":
        if self.timeout_seconds:
            self._timer = threading.Timer(self.timeout_seconds, self._on_timeout)
            self._timer.daemon = True
            self._timer.start()
        return self

    def __exit__(self, *exc) -> None:
        if self._timer is not None:
            self._timer.cancel()
