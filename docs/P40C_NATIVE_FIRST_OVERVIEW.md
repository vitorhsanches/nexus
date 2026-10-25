# P4.0C — Native-first runtime and Overview

The Overview is now independent of OmniRoute availability.  Its normal refresh
uses `/api/runtime/status`, a local capability probe for Codex Native and
Claude Native.  The probe does not start an App-Server, enumerate models, read
credential contents, or instantiate OmniRoute telemetry.

Codex Native reports `UNVERIFIED` when `auth.json` exists: its presence is
needed by the current session launcher but cannot prove a usable login. The
Codex App-Server validates authentication when the user starts a session.
Claude Native uses the CLI's `auth status --json` through a background probe;
the Overview never waits on that process. The result is cached for 30 seconds
and a probe exceeding 45 seconds is reported as `UNAVAILABLE`. No token,
e-mail, or credential payload is returned to the browser. The KPI counts only
native agents whose login has been confirmed, never OmniRoute providers.

`/api/routing/status` is still available for the existing OmniRoute-backed
Models & Routing panel.  It is intentionally queried only after the operator
opens that panel (or enters `models` / `routing` in the command palette).  A
normal Overview refresh shows OmniRoute as `NOT QUERIED` instead of treating a
telemetry outage as a Nexus runtime outage.

The legacy Run Mission action still requires an observed callable OmniRoute
route. It stays disabled until Models & Routing is opened explicitly; native
Chat and Development do not use this gate.

This keeps OmniRoute routes and gates intact while allowing native Chat and
Development sessions to be selected when OmniRoute is stopped or absent.
