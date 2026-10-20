"""Paths to version-controlled runtime assets used by Nexus subprocesses."""

from pathlib import Path


RUNTIME_ROOT = Path(__file__).resolve().parent / "runtime"
MANAGER_SKILL_SOURCE = RUNTIME_ROOT / "multi-agent-development-manager"
OMNIROUTE_WORKER_SCRIPT = MANAGER_SKILL_SOURCE / "scripts" / "omniroute-worker.ps1"
