"""Explicit Chat runtime contract and built-in runtime registry.

The contract is intentionally small: a runtime identifies itself and creates
a protocol-compatible conversation client. A future Claude Code adapter can
implement the same shape without changing Chat session/turn persistence or
the cockpit API; model/auth discovery remains adapter-specific orchestration.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from nexus.chat.appserver_client import (
    AppServerClient,
    AppServerError,
    CodexNativeAppServerClient,
)

OMNIROUTE_RUNTIME = "omniroute"
CODEX_NATIVE_RUNTIME = "codex-native"
CHAT_RUNTIME_ENV = "NEXUS_CHAT_RUNTIME"


class RuntimeClient(Protocol):
    codex_home: str | None

    def start(self) -> dict: ...
    def stop(self) -> None: ...


@dataclass(frozen=True)
class ChatRuntime:
    runtime_id: str
    display_name: str
    provider: str
    uses_local_cli_auth: bool
    client_factory: Callable[..., RuntimeClient]

    def create_client(self, repo: str | Path, **kwargs) -> RuntimeClient:
        return self.client_factory(repo, **kwargs)


_RUNTIMES = {
    OMNIROUTE_RUNTIME: ChatRuntime(
        runtime_id=OMNIROUTE_RUNTIME,
        display_name="OmniRoute",
        provider="omniroute",
        uses_local_cli_auth=False,
        client_factory=AppServerClient,
    ),
    CODEX_NATIVE_RUNTIME: ChatRuntime(
        runtime_id=CODEX_NATIVE_RUNTIME,
        display_name="Codex Native",
        provider="openai",
        uses_local_cli_auth=True,
        client_factory=CodexNativeAppServerClient,
    ),
}


def configured_runtime_id() -> str:
    runtime_id = os.environ.get(CHAT_RUNTIME_ENV, OMNIROUTE_RUNTIME).strip().lower()
    if runtime_id not in _RUNTIMES:
        raise AppServerError(
            "CHAT_RUNTIME_INVALID",
            f"{CHAT_RUNTIME_ENV} must be one of: {', '.join(sorted(_RUNTIMES))}.",
        )
    return runtime_id


def get_runtime(runtime_id: str | None = None) -> ChatRuntime:
    resolved = configured_runtime_id() if runtime_id is None else runtime_id.strip().lower()
    runtime = _RUNTIMES.get(resolved)
    if runtime is None:
        raise AppServerError(
            "CHAT_RUNTIME_NOT_SUPPORTED",
            f"Unsupported Chat runtime {runtime_id!r}; no fallback was attempted.",
        )
    return runtime


def public_runtime_catalog() -> dict:
    """Return capability metadata only; never credentials or account data."""
    default = configured_runtime_id()
    return {
        "default_runtime": default,
        "runtimes": [
            {
                "id": runtime.runtime_id,
                "name": runtime.display_name,
                "provider": runtime.provider,
                "uses_local_cli_auth": runtime.uses_local_cli_auth,
                "selected": runtime.runtime_id == default,
            }
            for runtime in _RUNTIMES.values()
        ],
    }


def assert_native_authenticated(client: RuntimeClient) -> str:
    """Validate local App-Server auth and return only its non-secret type."""
    account_read = getattr(client, "account_read", None)
    if not callable(account_read):
        raise AppServerError("CODEX_AUTH_UNAVAILABLE", "Codex App-Server cannot report authentication state.")
    try:
        result = account_read(refresh_token=False)
    except AppServerError as error:
        raise AppServerError(
            "CODEX_AUTH_UNAVAILABLE",
            "Codex CLI authentication could not be validated. Run `codex login` locally, then retry.",
            detail=error.detail,
        ) from error
    account = result.get("account") if isinstance(result, dict) else None
    requires_auth = result.get("requiresOpenaiAuth", True) if isinstance(result, dict) else True
    if requires_auth and not isinstance(account, dict):
        raise AppServerError(
            "CODEX_AUTH_UNAVAILABLE",
            "Codex CLI is not signed in. Run `codex login` locally, then retry.",
        )
    account_type = account.get("type") if isinstance(account, dict) else None
    return account_type if isinstance(account_type, str) and account_type else "managed"
