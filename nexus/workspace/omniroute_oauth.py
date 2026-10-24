"""Read OAuth connection state from OmniRoute's authenticated management CLI.

The public /v1/models catalog is deliberately not used as proof of a live
OAuth connection. Only a small allowlist of status fields leaves this module.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from urllib import request
from urllib.parse import urlsplit

PROVIDERS = {"claude_code": frozenset({"claude", "claude-code", "command-code"}),
             "codex": frozenset({"codex"})}
NAMES = {"claude_code": "Claude Code via OmniRoute", "codex": "OpenAI Codex via OmniRoute"}


def _management_connections() -> list[dict] | None:
    """Read the authenticated management API; never return or log raw records."""
    token = os.environ.get("OMNIROUTE_API_KEY")
    if not token:
        return None
    base = os.environ.get("OMNIROUTE_BASE_URL", "http://127.0.0.1:20128").rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return None
    if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        return None
    try:
        call = request.Request(base + "/api/providers", headers={
            "Authorization": "Bearer " + token, "Accept": "application/json"})
        with request.urlopen(call, timeout=4) as response:
            payload = json.loads(response.read(1_000_001))
        if isinstance(payload, dict):
            payload = payload.get("connections", payload.get("providers", payload.get("items")))
        return payload if isinstance(payload, list) else None
    except (OSError, ValueError, UnicodeError, TypeError):
        return None


def _cli_connections() -> list[dict] | None:
    executable = shutil.which("omniroute")
    if not executable:
        return None
    try:
        completed = subprocess.run(
            [executable, "--output", "json", "oauth", "status"],
            capture_output=True, text=True, timeout=8, check=False,
        )
        if completed.returncode != 0:
            return None
        connections = json.loads(completed.stdout)
        if isinstance(connections, dict):
            connections = connections.get("connections", connections.get("providers", connections.get("items")))
        return connections if isinstance(connections, list) else None
    except (OSError, subprocess.TimeoutExpired, UnicodeError, ValueError):
        return None


def connection_status() -> dict[str, dict]:
    result = {key: {"name": NAMES[key], "connected": False, "detail": "Conexão OAuth não confirmada pelo OmniRoute."}
              for key in PROVIDERS}
    connections = _management_connections()
    if connections is None:
        connections = _cli_connections()
    if connections is None:
        return result
    for key, aliases in PROVIDERS.items():
        # An active record with a failed or unknown test is not a proven
        # connection. Never return the management API's credential fields.
        live = any(
            isinstance(item, dict) and item.get("provider") in aliases
            and item.get("authType") in {"oauth", "oauth2"}
            and item.get("isActive") is True
            and str(item.get("testStatus", "")).lower() == "active"
            for item in connections
        )
        if live:
            result[key] = {"name": NAMES[key], "connected": True,
                           "detail": "OAuth ativo e testado pelo OmniRoute; inferência no Nexus não verificada."}
    return result


def require_connected_route(model_id: str) -> None:
    """Fail closed for new cc/cx work while retaining saved preferences."""
    provider = "claude_code" if model_id.startswith("cc/") else "codex" if model_id.startswith("cx/") else None
    if provider is None:
        return
    from nexus.dev.service import DevServiceError
    from nexus.registry import workspace_ai_providers

    if not workspace_ai_providers.read().get(provider, True) or not connection_status()[provider]["connected"]:
        raise DevServiceError("AI_PROVIDER_UNAVAILABLE", "Provedor OAuth desconectado ou inativo no Nexus.", http_status=409)


def filter_connected_routes(routes):
    """Keep unrelated legacy providers; gate cc/cx on one fresh status read."""
    from nexus.registry import workspace_ai_providers

    if not any(route.model_id.startswith(("cc/", "cx/")) for route in routes):
        return tuple(routes)
    connections = connection_status()
    switches = workspace_ai_providers.read()
    def allowed(route):
        provider = "claude_code" if route.model_id.startswith("cc/") else "codex" if route.model_id.startswith("cx/") else None
        return provider is None or (switches.get(provider, True) and connections[provider]["connected"])
    return tuple(route for route in routes if allowed(route))
