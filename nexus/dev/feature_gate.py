"""Server-side Development Mode EXECUTION feature gate (F2.2).

Separate from, and in addition to, per-project dev_enabled authorization
(nexus/registry/projects.py). dev_enabled answers "may THIS project be
developed on"; this gate answers a different, global question: "has Nexus
itself verified that starting a write-capable turn is currently safe to
expose at all." Read/list operations (sessions, turns, history) are never
gated by this -- only the one action that actually launches a Codex
process with workspace-write (nexus/dev/executor.py::run_dev_turn).

Why this exists and defaults OFF, with evidence (F2.2 Phase 1
investigation -- this is not a guess):

nexus/chat/appserver_client.py's own docstring already documents a
live-tested finding that the OmniRoute profile's config.toml (hardcoded
sandbox_mode="workspace-write") overrides the per-thread `sandbox`
JSON-RPC parameter -- only AppServerClient's process-level
`sandbox_mode_override` actually works. The SAME config.toml also
hardcodes approval_policy="never", with no equivalent process-level
override mechanism for it.

A dedicated live diagnostic for F2.2 (real AppServerClient, real
OmniRoute, disposable repo, a REAL on_server_request handler that logs
every call it receives and still always denies) asked a real Codex agent
to attempt a network-requiring command under
thread_start(approval_policy="on-request") -- exactly the kind of action
"on-request" is supposed to pause for. The command was silently blocked
by the sandbox at the connection level, and the logging handler received
ZERO approval or permission requests. This is a POSITIVE finding that the
on-request protocol path is NOT exercised here, not merely an absence of
evidence -- see Phase 1 requirement 8 ("do not interpret the absence of
approval callbacks as proof of a safe approval policy"): the correct
reading of "zero requests" combined with "the risky action was attempted
and still blocked" is that the *sandbox* (independently, technically
enforced -- already verified in F2.1 for filesystem writes, and again
here for network) is the only real boundary; the advertised human-
approval checkpoint is not an active control.

Because of that, this module hardcodes the gate OFF rather than wiring
any "enable via API" path: enabling it would let the Web Cockpit present
an "approve or deny this action" control to a user when the backend
protocol never actually offers that choice -- which would be misleading,
not merely incomplete. The only way to turn it on is the operator
explicitly setting NEXUS_DEV_EXECUTION_ENABLED=1 in Nexus's own process
environment before starting the server -- never a request parameter,
never a UI toggle, never inferred from configuration being merely
present. This mirrors nexus.registry.projects.dev_enabled's own
"disabled by default, revalidated every call, never silently turned on"
discipline one level up.
"""

from __future__ import annotations

import os

ENV_VAR = "NEXUS_DEV_EXECUTION_ENABLED"


def execution_enabled() -> bool:
    """Re-read every call -- never cached, so flipping the env var for a
    running process (restart required in practice, since os.environ is
    the only source) or a test's monkeypatch takes effect immediately."""
    return os.environ.get(ENV_VAR) == "1"
