import json
import re
from pathlib import Path

from nexus.registry.database import get_connection


ROOT_DIR = Path(__file__).resolve().parents[2]
PROJECTS_FILE = ROOT_DIR / "config" / "projects.json"
LOCAL_PROJECTS_FILE = ROOT_DIR / "config" / "projects.local.json"


def load_project_entries() -> list[dict]:
    """Load the safe shared defaults plus machine-local project overrides."""
    entries: dict[str, dict] = {}
    for config_path in (PROJECTS_FILE, LOCAL_PROJECTS_FILE):
        # Tests and integrations that point PROJECTS_FILE at a fixture should
        # remain isolated from the real machine-local overlay.
        if config_path == LOCAL_PROJECTS_FILE and PROJECTS_FILE != ROOT_DIR / "config" / "projects.json":
            continue
        if not config_path.exists():
            continue
        with config_path.open("r", encoding="utf-8-sig") as file:
            payload = json.load(file)
        for project in payload.get("projects", []):
            entries[project["id"]] = project
    return list(entries.values())


def sync_projects() -> None:
    projects = load_project_entries()
    # A missing config is different from a present config with an empty
    # inventory: the latter is an explicit request to disable stale rows.
    if not PROJECTS_FILE.exists() and not LOCAL_PROJECTS_FILE.exists():
        return
    configured_ids = [project["id"] for project in projects]

    with get_connection() as connection:
        for project in projects:
            connection.execute(
                """
                INSERT INTO projects (
                    id,
                    name,
                    path,
                    aliases_json,
                    enabled
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    path = excluded.path,
                    aliases_json = excluded.aliases_json,
                    enabled = excluded.enabled,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    project["id"],
                    project["name"],
                    str(_resolve_project_path(project["path"])),
                    json.dumps(project.get("aliases", []), ensure_ascii=False),
                    1 if project.get("enabled", True) else 0,
                ),
            )
        if configured_ids:
            placeholders = ", ".join("?" for _ in configured_ids)
            connection.execute(
                f"""
                UPDATE projects
                SET enabled = 0,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id NOT IN ({placeholders})
                """,
                configured_ids,
            )
        else:
            connection.execute(
                """
                UPDATE projects
                SET enabled = 0,
                    updated_at = CURRENT_TIMESTAMP
                """
            )


def _resolve_project_path(value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (ROOT_DIR / path).resolve()


_ID_CHARS = re.compile(r"[^a-z0-9]+")


def _slugify(name: str) -> str:
    slug = _ID_CHARS.sub("-", name.strip().lower()).strip("-")
    return slug or "project"


def register_local_project(name: str, path: str) -> dict:
    """Validate and persist one new project into config/projects.local.json,
    preserving every entry already there, then reconcile it into the
    registry immediately -- no Nexus restart needed to see it.

    Read-only Git check: only confirms a ``.git`` entry exists under the
    path. Never runs ``git init``, ``checkout``, or any other Git command.
    The new row is never given Development authorization (dev_enabled is
    left at the schema default, untouched by sync_projects()) and no
    global execution flag is touched here.
    """
    from nexus.dev.service import DevServiceError  # local import: avoids the registry/dev import cycle

    clean_name = (name or "").strip()
    if not clean_name:
        raise DevServiceError("PROJECT_NAME_REQUIRED", "Informe um nome para o projeto.", http_status=400)
    raw_path = (path or "").strip()
    if not raw_path:
        raise DevServiceError("PROJECT_PATH_REQUIRED", "Informe o caminho local do repositório.", http_status=400)
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        raise DevServiceError("PROJECT_PATH_NOT_ABSOLUTE", "Use um caminho absoluto para o repositório.", http_status=400)
    if not candidate.exists() or not candidate.is_dir():
        raise DevServiceError("PROJECT_PATH_NOT_FOUND", f"Caminho não encontrado: {raw_path!r}", http_status=400)
    resolved = candidate.resolve()
    if not (resolved / ".git").exists():
        raise DevServiceError(
            "PROJECT_PATH_NOT_GIT_REPOSITORY",
            f"O caminho não é um repositório Git (sem .git): {raw_path!r}",
            http_status=400,
        )

    entries = load_project_entries()
    existing_ids = {entry["id"] for entry in entries}
    existing_paths = {str(_resolve_project_path(entry["path"])) for entry in entries}
    if str(resolved) in existing_paths:
        raise DevServiceError(
            "PROJECT_PATH_ALREADY_REGISTERED",
            f"Este repositório já está cadastrado: {resolved}",
            http_status=409,
        )

    base_id = _slugify(clean_name)
    project_id = base_id
    suffix = 2
    while project_id in existing_ids:
        project_id = f"{base_id}-{suffix}"
        suffix += 1

    local_payload: dict = {"projects": []}
    if LOCAL_PROJECTS_FILE.exists():
        with LOCAL_PROJECTS_FILE.open("r", encoding="utf-8-sig") as file:
            local_payload = json.load(file)
    local_projects = list(local_payload.get("projects", []))
    local_projects.append({
        "id": project_id, "name": clean_name, "path": str(resolved), "aliases": [], "enabled": True,
    })
    local_payload["projects"] = local_projects
    LOCAL_PROJECTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = LOCAL_PROJECTS_FILE.with_name(LOCAL_PROJECTS_FILE.name + ".tmp")
    tmp_path.write_text(json.dumps(local_payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp_path.replace(LOCAL_PROJECTS_FILE)

    sync_projects()
    with get_connection() as connection:
        row = connection.execute(
            "SELECT id, name, path, enabled, dev_enabled FROM projects WHERE id = ?", (project_id,),
        ).fetchone()
    return dict(row)


def list_projects():
    with get_connection() as connection:
        return connection.execute(
            """
            SELECT id, name, path, enabled, dev_enabled
            FROM projects
            ORDER BY name
            """
        ).fetchall()


def get_project(project_id: str):
    with get_connection() as connection:
        return connection.execute(
            """
            SELECT id, name, path, enabled
            FROM projects
            WHERE id = ?
            """,
            (project_id,),
        ).fetchone()


def get_dev_enabled(project_id: str) -> bool:
    """Development Mode authorization for one project. False (and never an
    error) for an unknown project_id -- absence of a row is never implicit
    permission."""
    with get_connection() as connection:
        row = connection.execute(
            "SELECT dev_enabled FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
    return bool(row["dev_enabled"]) if row is not None else False


def set_dev_enabled(project_id: str, enabled: bool) -> None:
    """Internal-only setter (F1.2): no route/endpoint calls this yet. Never
    touched by sync_projects(), so re-syncing config/projects.json can
    never silently change it."""
    with get_connection() as connection:
        connection.execute(
            "UPDATE projects SET dev_enabled = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (1 if enabled else 0, project_id),
        )


def list_projects_for_routing() -> list[dict]:
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT id, name, path, aliases_json, enabled
            FROM projects
            """
        ).fetchall()

    return [
        {
            "id": row["id"],
            "name": row["name"],
            "path": row["path"],
            "aliases": json.loads(row["aliases_json"] or "[]"),
            "enabled": bool(row["enabled"]),
        }
        for row in rows
    ]
