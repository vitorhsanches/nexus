import json
from pathlib import Path

from nexus.registry.database import get_connection


ROOT_DIR = Path(__file__).resolve().parents[2]
PROJECTS_FILE = ROOT_DIR / "config" / "projects.json"


def sync_projects() -> None:
    if not PROJECTS_FILE.exists():
        return

    with PROJECTS_FILE.open("r", encoding="utf-8-sig") as file:
        payload = json.load(file)

    projects = payload.get("projects", [])

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
                    project["path"],
                    json.dumps(project.get("aliases", []), ensure_ascii=False),
                    1 if project.get("enabled", True) else 0,
                ),
            )


def list_projects():
    with get_connection() as connection:
        return connection.execute(
            """
            SELECT id, name, path, enabled
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
