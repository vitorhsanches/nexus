"""Native-first runtime status for the Nexus Overview.

This module is intentionally a local capability probe.  It never imports the
OmniRoute telemetry client, starts an App-Server, or discovers model catalogs.
The Overview needs to know whether the native CLIs can be selected; detailed
model/routing telemetry remains an explicit, operator-requested action.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Callable

from nexus.chat import claude_native
from nexus.dispatchers.manager import _find_codex


def _codex_auth_path() -> Path:
    explicit = os.environ.get("NEXUS_NATIVE_CODEX_AUTH_HOME") or os.environ.get("CODEX_HOME")
    if explicit:
        return Path(explicit) / "auth.json"
    profile = os.environ.get("USERPROFILE") or os.environ.get("HOME") or ""
    return Path(profile) / ".codex" / "auth.json"


def _codex_status(*, finder: Callable[[], object] = _find_codex,
                  auth_path: Callable[[], Path] = _codex_auth_path) -> dict:
    try:
        finder()
    except FileNotFoundError:
        return {
            "id": "codex_native",
            "label": "Codex Native",
            "status": "NOT_INSTALLED",
            "installed": False,
            "authenticated": False,
            "source": "local-cli",
            "error": "Codex CLI was not found.",
        }
    except Exception as error:
        return {
            "id": "codex_native",
            "label": "Codex Native",
            "status": "UNAVAILABLE",
            "installed": False,
            "authenticated": None,
            "source": "local-cli",
            "error": str(error)[:240],
        }

    try:
        has_auth_file = auth_path().is_file()
    except OSError as error:
        return {
            "id": "codex_native",
            "label": "Codex Native",
            "status": "UNAVAILABLE",
            "installed": True,
            "authenticated": None,
            "source": "local-cli",
            "error": f"Codex login status is unavailable: {error}",
        }
    return {
        "id": "codex_native",
        "label": "Codex Native",
        # The App-Server verifies the login when a session starts. A file can
        # be expired, malformed, or revoked, so it never proves readiness.
        "status": "UNVERIFIED" if has_auth_file else "AUTH_REQUIRED",
        "installed": True,
        "authenticated": None if has_auth_file else False,
        "source": "local-file-check",
        "error": None if has_auth_file else "Codex is not signed in. Run 'codex login'.",
    }


def _claude_status(status_reader: Callable[[], dict] = claude_native.cli_status) -> dict:
    try:
        status = status_reader()
    except Exception as error:
        return {
            "id": "claude_native",
            "label": "Claude Native",
            "status": "UNAVAILABLE",
            "installed": False,
            "authenticated": None,
            "source": "local-cli",
            "error": f"Claude Code status is unavailable: {error}",
        }
    installed = bool(status.get("installed"))
    authenticated = bool(status.get("logged_in")) if installed else False
    if not installed:
        state = "NOT_INSTALLED"
    elif status.get("error"):
        state = "UNAVAILABLE"
    elif not authenticated:
        state = "AUTH_REQUIRED"
    else:
        state = "READY"
    return {
        "id": "claude_native",
        "label": "Claude Native",
        "status": state,
        "installed": installed,
        "authenticated": authenticated,
        "source": "local-cli",
        "error": status.get("error") or (
            "Claude Code is not signed in. Run 'claude auth login'."
            if installed and not authenticated else None
        ),
        "version": status.get("version"),
    }


CLAUDE_STATUS_TTL_SECONDS = 30
# cli_status makes two subprocess calls with 20s timeouts. Keep a margin for
# process startup, while never making an Overview request wait for either one.
CLAUDE_STATUS_BUDGET_SECONDS = 45


class ClaudeStatusCache:
    """One bounded background status probe shared by Overview requests."""

    def __init__(self, reader: Callable[[], dict] = claude_native.cli_status,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._reader = reader
        self._clock = clock
        self._lock = threading.Lock()
        self._result: dict | None = None
        self._expires_at = 0.0
        self._worker: threading.Thread | None = None
        self._started_at = 0.0

    def get(self) -> dict:
        with self._lock:
            now = self._clock()
            if self._result is not None and now < self._expires_at:
                return dict(self._result)
            if self._worker is None:
                self._started_at = now
                self._worker = threading.Thread(target=self._refresh, daemon=True)
                self._worker.start()
            elif now - self._started_at >= CLAUDE_STATUS_BUDGET_SECONDS:
                return self._pending("UNAVAILABLE", "Claude Code status check timed out.")
            return self._pending("CHECKING", None)

    @staticmethod
    def _pending(status: str, error: str | None) -> dict:
        return {"id": "claude_native", "label": "Claude Native", "status": status,
                "installed": None, "authenticated": None, "source": "local-cli", "error": error}

    def _refresh(self) -> None:
        result = _claude_status(self._reader)
        with self._lock:
            if self._clock() - self._started_at >= CLAUDE_STATUS_BUDGET_SECONDS:
                result = self._pending("UNAVAILABLE", "Claude Code status check timed out.")
            self._result = result
            self._expires_at = self._clock() + CLAUDE_STATUS_TTL_SECONDS
            self._worker = None


_claude_status_cache = ClaudeStatusCache()


def get_native_runtime_status(*, codex_probe: Callable[[], dict] | None = None,
                              claude_probe: Callable[[], dict] | None = None) -> dict:
    """Return a secret-free native status snapshot for the Overview.

    ``omniroute`` is reported as ``NOT_QUERIED`` by design.  The response is
    safe to call on every Overview refresh and remains useful when OmniRoute is
    stopped, uninstalled, or unreachable.
    """
    codex = codex_probe() if codex_probe else _codex_status()
    claude = claude_probe() if claude_probe else _claude_status_cache.get()
    ready = [item for item in (codex, claude) if item.get("status") == "READY"]
    statuses = {item.get("status") for item in (codex, claude)}
    if ready:
        native_status = "READY"
    else:
        native_status = next(
            (status for status in ("CHECKING", "UNVERIFIED", "AUTH_REQUIRED", "UNAVAILABLE")
             if status in statuses),
            "NOT_INSTALLED",
        )
    return {
        "default_runtime": "native",
        "native_first": True,
        "native_ready": bool(ready),
        "native_status": native_status,
        "runtimes": {"codex_native": codex, "claude_native": claude},
        "omniroute": {
            "status": "NOT_QUERIED",
            "reason": "OmniRoute telemetry is only collected after opening Models & Routing.",
        },
    }
