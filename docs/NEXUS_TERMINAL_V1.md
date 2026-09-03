# Nexus Terminal V1

## Objective

Create an interactive command interface for Nexus orchestration.

The terminal must allow the user to submit natural language requests and observe the complete lifecycle of AI agents.

---

# Current Architecture

User
 |
Nexus CLI
 |
Project Router
 |
Luna Manager
 |
NEXUS_PLAN
 |
Progressive Executor
 |
Workers
 |
Review
 |
Run Registry


---

# Terminal Command

Initial command:

python -m nexus terminal


Example:

nexus> Norte: fix dashboard bug


---

# Core Responsibilities

The terminal must:

- receive natural language commands;
- create Runs;
- display execution status;
- show active Agents;
- display Worker routing;
- display Review status;
- show final result.


---

# Commands

Initial commands:

terminal

Inside terminal:

go <request>

runs

agents

show <run_id>

exit


---

# Event Model

Terminal should consume orchestration events:

RUN_CREATED

MANAGER_STARTED

PLAN_CREATED

WORKER_STARTED

WORKER_COMPLETED

REVIEW_STARTED

REVIEW_COMPLETED

RUN_COMPLETED

RUN_FAILED


---

# Model Visibility

The user should see:

Manager:
- model
- status

Worker:
- provider
- model
- role
- status

Review:
- reviewer
- verdict


---

# Future

Possible evolution:

- Rich terminal UI
- Web dashboard
- Multi-run monitoring
- Cost tracking
- Token accounting