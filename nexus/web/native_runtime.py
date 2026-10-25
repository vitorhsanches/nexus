"""Native-first runtime status for the Nexus Overview.

This module is intentionally a local capability probe.  It never imports the
OmniRoute telemetry client, starts an App-Server, or discovers model catalogs.
The Overview needs to know whether the native CLIs can be selected; detailed
model/routing telemetry remains an explicit, operator-requested action.
"""

from __future__ import annotations

import os
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
        logged_in = auth_path().is_file()
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
        "status": "READY" if logged_in else "AUTH_REQUIRED",
        "installed": True,
        "authenticated": logged_in,
        "source": "local-cli",
        "error": None if logged_in else "Codex is not signed in. Run 'codex login'.",
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


def get_native_runtime_status(*, codex_probe: Callable[[], dict] | None = None,
                              claude_probe: Callable[[], dict] | None = None) -> dict:
    """Return a secret-free native status snapshot for the Overview.

    ``omniroute`` is reported as ``NOT_QUERIED`` by design.  The response is
    safe to call on every Overview refresh and remains useful when OmniRoute is
    stopped, uninstalled, or unreachable.
    """
    codex = codex_probe() if codex_probe else _codex_status()
    claude = claude_probe() if claude_probe else _claude_status()
    ready = [item for item in (codex, claude) if item.get("status") == "READY"]
    return {
        "default_runtime": "native",
        "native_first": True,
        "native_ready": bool(ready),
        "runtimes": {"codex_native": codex, "claude_native": claude},
        "omniroute": {
            "status": "NOT_QUERIED",
            "reason": "OmniRoute telemetry is only collected after opening Models & Routing.",
        },
    }
