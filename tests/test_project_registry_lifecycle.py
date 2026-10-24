"""Regression tests for configured project registry reconciliation."""

import json
from pathlib import Path

import nexus.registry.database as database
import nexus.registry.projects as projects
import pytest
from nexus.registry.runs import create_run, get_run
from nexus.router import ProjectNotFoundError, resolve_project


def project_entry(project_id="alpha", path=r"C:\workspace\alpha", alias="alpha"):
    return {
        "id": project_id,
        "name": project_id.title(),
        "path": path,
        "aliases": [alias],
        "enabled": True,
    }


@pytest.fixture
def isolated_registry(tmp_path):
    original_db_path = database.DATABASE_PATH
    original_storage_dir = database.STORAGE_DIR
    original_projects_file = projects.PROJECTS_FILE
    original_local_projects_file = projects.LOCAL_PROJECTS_FILE
    database.STORAGE_DIR = tmp_path / "storage"
    database.DATABASE_PATH = database.STORAGE_DIR / "nexus.db"
    projects.PROJECTS_FILE = tmp_path / "projects.json"
    projects.LOCAL_PROJECTS_FILE = tmp_path / "projects.local.json"
    database.initialize_database()
    try:
        yield tmp_path
    finally:
        database.DATABASE_PATH = original_db_path
        database.STORAGE_DIR = original_storage_dir
        projects.PROJECTS_FILE = original_projects_file
        projects.LOCAL_PROJECTS_FILE = original_local_projects_file


def write_config(path: Path, entries: list[dict]) -> None:
    path.write_text(json.dumps({"projects": entries}), encoding="utf-8")


def db_project(project_id: str):
    with database.get_connection() as connection:
        return connection.execute(
            "SELECT id, name, path, aliases_json, enabled FROM projects WHERE id = ?",
            (project_id,),
        ).fetchone()


def test_configured_project_is_inserted_and_enabled(isolated_registry):
    write_config(projects.PROJECTS_FILE, [project_entry()])
    projects.sync_projects()
    assert dict(db_project("alpha"))["enabled"] == 1
    assert resolve_project("alpha").id == "alpha"


def test_new_install_without_any_project_config_is_empty_and_safe(isolated_registry):
    projects.sync_projects()

    with database.get_connection() as connection:
        rows = connection.execute("SELECT id FROM projects").fetchall()
    assert rows == []


def test_present_project_is_updated_on_sync(isolated_registry):
    write_config(projects.PROJECTS_FILE, [project_entry(path=r"C:\old")])
    projects.sync_projects()
    write_config(projects.PROJECTS_FILE, [project_entry(path=r"C:\new", alias="new-alpha")])
    projects.sync_projects()
    row = dict(db_project("alpha"))
    assert row["enabled"] == 1
    assert row["path"] == r"C:\new"
    assert resolve_project("new-alpha").id == "alpha"


def test_removed_project_is_disabled_and_alias_stops_resolving(isolated_registry):
    write_config(projects.PROJECTS_FILE, [project_entry(alias="legacy-alpha")])
    projects.sync_projects()
    write_config(projects.PROJECTS_FILE, [])
    projects.sync_projects()
    assert dict(db_project("alpha"))["enabled"] == 0
    with pytest.raises(ProjectNotFoundError):
        resolve_project("legacy-alpha")


def test_disabled_project_id_and_name_stop_resolving(isolated_registry):
    write_config(projects.PROJECTS_FILE, [project_entry()])
    projects.sync_projects()
    write_config(projects.PROJECTS_FILE, [])
    projects.sync_projects()
    for query in ("alpha", "Alpha"):
        with pytest.raises(ProjectNotFoundError):
            resolve_project(query)


def test_historical_run_remains_readable_after_disable(isolated_registry):
    write_config(projects.PROJECTS_FILE, [project_entry()])
    projects.sync_projects()
    run_id = create_run("alpha", "historical request")
    write_config(projects.PROJECTS_FILE, [])
    projects.sync_projects()
    assert get_run(run_id)["project_id"] == "alpha"
    assert dict(db_project("alpha"))["enabled"] == 0


def test_readding_project_reactivates_and_updates_projection(isolated_registry):
    write_config(projects.PROJECTS_FILE, [project_entry()])
    projects.sync_projects()
    write_config(projects.PROJECTS_FILE, [])
    projects.sync_projects()
    write_config(projects.PROJECTS_FILE, [project_entry(path=r"C:\new", alias="new-alpha")])
    projects.sync_projects()
    row = dict(db_project("alpha"))
    assert row["enabled"] == 1
    assert row["path"] == r"C:\new"
    assert resolve_project("new-alpha").id == "alpha"


def test_sync_is_idempotent(isolated_registry):
    entries = [project_entry(), project_entry("beta", r"C:\workspace\beta")]
    write_config(projects.PROJECTS_FILE, entries)
    projects.sync_projects()
    with database.get_connection() as connection:
        first = [tuple(row) for row in connection.execute(
            "SELECT id, path, aliases_json, enabled FROM projects ORDER BY id"
        )]
    projects.sync_projects()
    with database.get_connection() as connection:
        second = [tuple(row) for row in connection.execute(
            "SELECT id, path, aliases_json, enabled FROM projects ORDER BY id"
        )]
    assert first == second


def test_empty_config_disables_all_projection_rows(isolated_registry):
    write_config(projects.PROJECTS_FILE, [project_entry(), project_entry("beta")])
    projects.sync_projects()
    write_config(projects.PROJECTS_FILE, [])
    projects.sync_projects()
    with database.get_connection() as connection:
        rows = connection.execute("SELECT enabled FROM projects ORDER BY id").fetchall()
    assert [row["enabled"] for row in rows] == [0, 0]


def test_nonexistent_stale_path_cannot_win_after_removal(isolated_registry):
    write_config(projects.PROJECTS_FILE, [project_entry(path=r"C:\does-not-exist")])
    projects.sync_projects()
    assert resolve_project("alpha").path == r"C:\does-not-exist"
    write_config(projects.PROJECTS_FILE, [])
    projects.sync_projects()
    with pytest.raises(ProjectNotFoundError):
        resolve_project("alpha")


def test_no_inventory_specific_lifecycle_logic(isolated_registry):
    write_config(projects.PROJECTS_FILE, [project_entry("inventory-lite-e2e-4")])
    projects.sync_projects()
    write_config(projects.PROJECTS_FILE, [])
    projects.sync_projects()

    assert dict(db_project("inventory-lite-e2e-4"))["enabled"] == 0


def test_local_config_adds_projects_and_overrides_shared_relative_paths(
    isolated_registry, tmp_path
):
    projects.PROJECTS_FILE = (
        Path(__file__).resolve().parents[1] / "config" / "projects.json"
    )
    write_config(
        projects.LOCAL_PROJECTS_FILE,
        [
            project_entry("nexus", path="portable-nexus-root"),
            project_entry("personal", path=str(tmp_path / "personal")),
        ],
    )

    projects.sync_projects()

    assert dict(db_project("nexus"))["path"] == str(
        (projects.ROOT_DIR / "portable-nexus-root").resolve()
    )
    assert dict(db_project("personal"))["path"] == str(tmp_path / "personal")


def test_shared_relative_path_resolves_from_repository_root(isolated_registry):
    projects.PROJECTS_FILE = (
        Path(__file__).resolve().parents[1] / "config" / "projects.json"
    )

    projects.sync_projects()

    assert dict(db_project("nexus"))["path"] == str(projects.ROOT_DIR.resolve())
