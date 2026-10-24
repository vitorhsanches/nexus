"""Create and register a brand-new project repository for a Program.

The integration pipeline needs a git repository with at least one commit
(it branches a worktree from HEAD and fast-forwards back into it), and the
Project Router loads shared defaults from config/projects.json and
machine-local registrations from config/projects.local.json.
Bootstrap provides both, and stores the user's requirements prompt as
REQUIREMENTS.md so every later Manager/Worker reads it from the repo instead
of receiving it on a length-limited command line.
"""

from __future__ import annotations

import json
import re
import subprocess
import unicodedata
from pathlib import Path

from nexus.registry.projects import (
    LOCAL_PROJECTS_FILE,
    PROJECTS_FILE,
    load_project_entries,
    sync_projects,
)


REQUIREMENTS_FILE = "REQUIREMENTS.md"
DEFAULT_WORKSPACE_ROOT = Path(__file__).resolve().parents[3]

_BASE_GITIGNORE = """\
node_modules/
dist/
build/
.venv/
venv/
__pycache__/
*.py[cod]
.env
.env.local
.DS_Store
coverage/
"""


class BootstrapError(RuntimeError):
    pass


def slugify(name: str) -> str:
    value = unicodedata.normalize("NFKD", name)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value.lower()).strip("-")
    if not value:
        raise BootstrapError(f"Project name {name!r} yields an empty slug.")
    return value


def git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise BootstrapError(
            f"git {' '.join(args[:3])} failed in {cwd}: {result.stderr.strip()[-500:]}"
        )
    return result


def _identity_args(cwd: Path) -> list[str]:
    """Supply a commit identity only when git has none configured."""
    probe = subprocess.run(
        ["git", "config", "user.email"],
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )
    if probe.returncode == 0 and probe.stdout.strip():
        return []
    return ["-c", "user.name=Nexus", "-c", "user.email=nexus@localhost"]


def commit_all(repo: Path, message: str) -> str | None:
    """Stage everything and commit; return the SHA, or None if clean."""
    git(["add", "-A"], repo)
    status = git(["status", "--porcelain"], repo)
    if not status.stdout.strip():
        return None
    git([*_identity_args(repo), "commit", "--no-verify", "-m", message], repo)
    return git(["rev-parse", "HEAD"], repo).stdout.strip()


def register_project(project_id: str, name: str, path: Path) -> None:
    if any(p.get("id") == project_id for p in load_project_entries()):
        raise BootstrapError(f"Project id {project_id!r} is already registered.")

    payload = {"projects": []}
    if LOCAL_PROJECTS_FILE.exists():
        payload = json.loads(LOCAL_PROJECTS_FILE.read_text(encoding="utf-8-sig"))

    projects = payload.setdefault("projects", [])

    projects.append(
        {
            "id": project_id,
            "name": name,
            "path": str(path),
            "aliases": [project_id],
            "enabled": True,
        }
    )
    LOCAL_PROJECTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOCAL_PROJECTS_FILE.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    sync_projects()


def bootstrap_project(
    name: str,
    requirements: str,
    workspace_root: Path | None = None,
) -> dict:
    """Create ``<workspace_root>/<slug>`` as a committed git repo and register it."""
    if not requirements.strip():
        raise BootstrapError("Requirements prompt is empty.")

    project_id = slugify(name)
    root = Path(workspace_root or DEFAULT_WORKSPACE_ROOT)
    repo = root / project_id

    if repo.exists() and any(repo.iterdir()):
        raise BootstrapError(
            f"Target directory {repo} already exists and is not empty."
        )

    repo.mkdir(parents=True, exist_ok=True)
    git(["init", "-b", "main"], repo)

    (repo / REQUIREMENTS_FILE).write_text(
        f"# {name} — Requirements\n\n{requirements.strip()}\n",
        encoding="utf-8",
    )
    (repo / ".gitignore").write_text(_BASE_GITIGNORE, encoding="utf-8")
    sha = commit_all(repo, "chore: bootstrap project from Nexus requirements")

    register_project(project_id, name, repo)

    return {"project_id": project_id, "path": str(repo), "initial_commit": sha}
