# Nexus Intent Router V1

## Objective

Add intent classification before Manager execution.

The goal is to allow Nexus to distinguish between requests that require Workers and requests that can be completed directly by the Manager.

---

# Current limitation

Today every GO request expects a NEXUS_PLAN containing Workers.

This prevents pure analysis or planning requests.

Example:

Input:
"Analyze the interface-life project"

Expected:
Manager response only.

Current:
PLAN_INVALID because workers=[].

---

# Intent Types

## EXECUTION

Requires Worker execution.

Examples:

- fix a bug;
- modify code;
- implement feature;
- create automation.

Flow:

User
 |
Intent Router
 |
Manager
 |
Workers
 |
Review
 |
Complete


---

## ANALYSIS

No Worker required.

Examples:

- analyze architecture;
- review project;
- explain risks;
- suggest improvements.

Flow:

User
 |
Intent Router
 |
Manager
 |
Result
 |
Complete


---

## QUESTION

Direct answer request.

Examples:

- explain how X works;
- compare technologies.

---

## PLANNING

Future implementation planning.

Examples:

- create roadmap;
- design architecture.

---

# NEXUS_PLAN changes

Current:

{
 complexity,
 workers[]
}

New:

{
 complexity,
 intent,
 workers[]
}

Rules:

EXECUTION:
- workers required.

ANALYSIS:
- workers optional.

QUESTION:
- workers forbidden.

PLANNING:
- workers optional.

---

# Validation

The Router must be deterministic.

No external model call should be required.

Initial classification may use:

- explicit keywords;
- request structure;
- project context.

Future:
Manager can override classification.