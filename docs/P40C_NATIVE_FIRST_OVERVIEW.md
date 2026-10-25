# P4.0C — Native-first runtime and Overview

The Overview is now independent of OmniRoute availability.  Its normal refresh
uses `/api/runtime/status`, a local capability probe for Codex Native and
Claude Native.  The probe does not start an App-Server, enumerate models, read
credential contents, or instantiate OmniRoute telemetry.

The Overview labels the native runtime as `READY`, `AUTH_REQUIRED`, or
`NOT_INSTALLED`.  Authentication details remain in the local CLI; no token,
e-mail, or credential payload is returned to the browser.

`/api/routing/status` is still available for the existing OmniRoute-backed
Models & Routing panel.  It is intentionally queried only after the operator
opens that panel (or enters `models` / `routing` in the command palette).  A
normal Overview refresh shows OmniRoute as `NOT QUERIED` instead of treating a
telemetry outage as a Nexus runtime outage.

This keeps OmniRoute routes and gates intact while allowing native Chat and
Development sessions to be selected when OmniRoute is stopped or absent.
