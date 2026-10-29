"""P6.C2: local Git repository registration via the cockpit (register_local_project
and POST /api/projects) -- cadastro, duplicidade, caminho invalido and ausencia de
autorizacao, all against temporary data."""

import json
import subprocess
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.requests import Request

import nexus.registry.database as database
import nexus.registry.projects as projects
from nexus.dev.service import DevServiceError
from nexus.registry.projects import register_local_project


@pytest.fixture
def isolated_registry(tmp_path):
    """Isolates the DB and the machine-local overlay in temp dirs. PROJECTS_FILE
    is deliberately left pointed at the real config/projects.json: load_project_entries()
    only reads LOCAL_PROJECTS_FILE when PROJECTS_FILE is the real file (so tests that
    repoint PROJECTS_FILE elsewhere can't leak a developer's real local overlay),
    which register_local_project() relies on to see what it just wrote."""
    original_db_path = database.DATABASE_PATH
    original_storage_dir = database.STORAGE_DIR
    original_projects_file = projects.PROJECTS_FILE
    original_local_projects_file = projects.LOCAL_PROJECTS_FILE
    database.STORAGE_DIR = tmp_path / "storage"
    database.DATABASE_PATH = database.STORAGE_DIR / "nexus.db"
    projects.PROJECTS_FILE = Path(__file__).resolve().parents[1] / "config" / "projects.json"
    projects.LOCAL_PROJECTS_FILE = tmp_path / "projects.local.json"
    database.initialize_database()
    try:
        yield tmp_path
    finally:
        database.DATABASE_PATH = original_db_path
        database.STORAGE_DIR = original_storage_dir
        projects.PROJECTS_FILE = original_projects_file
        projects.LOCAL_PROJECTS_FILE = original_local_projects_file


def make_git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def test_register_persists_without_restart_and_starts_without_dev_authorization(
    isolated_registry, tmp_path
):
    repo = make_git_repo(tmp_path / "repo-a")
    result = register_local_project("Repo A", str(repo))

    assert result["name"] == "Repo A"
    assert result["path"] == str(repo.resolve())
    assert result["enabled"] == 1
    assert result["dev_enabled"] == 0

    with projects.LOCAL_PROJECTS_FILE.open("r", encoding="utf-8") as file:
        local_payload = json.load(file)
    assert [entry["id"] for entry in local_payload["projects"]] == [result["id"]]

    with database.get_connection() as connection:
        row = connection.execute(
            "SELECT enabled, dev_enabled FROM projects WHERE id = ?", (result["id"],)
        ).fetchone()
    assert dict(row) == {"enabled": 1, "dev_enabled": 0}


def test_register_preserves_existing_local_projects(isolated_registry, tmp_path):
    first_repo = make_git_repo(tmp_path / "repo-a")
    second_repo = make_git_repo(tmp_path / "repo-b")
    register_local_project("Repo A", str(first_repo))
    register_local_project("Repo B", str(second_repo))

    with projects.LOCAL_PROJECTS_FILE.open("r", encoding="utf-8") as file:
        local_payload = json.load(file)
    ids = {entry["id"] for entry in local_payload["projects"]}
    assert ids == {"repo-a", "repo-b"}


def test_duplicate_path_is_rejected(isolated_registry, tmp_path):
    repo = make_git_repo(tmp_path / "repo-a")
    register_local_project("Repo A", str(repo))

    with pytest.raises(DevServiceError) as error:
        register_local_project("Repo A Again", str(repo))
    assert error.value.code == "PROJECT_PATH_ALREADY_REGISTERED"


def test_nonexistent_path_is_rejected(isolated_registry, tmp_path):
    missing = tmp_path / "does-not-exist"
    with pytest.raises(DevServiceError) as error:
        register_local_project("Ghost", str(missing))
    assert error.value.code == "PROJECT_PATH_NOT_FOUND"


def test_non_git_directory_is_rejected(isolated_registry, tmp_path):
    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()
    with pytest.raises(DevServiceError) as error:
        register_local_project("Plain", str(plain_dir))
    assert error.value.code == "PROJECT_PATH_NOT_GIT_REPOSITORY"


def test_relative_path_is_rejected(isolated_registry, tmp_path):
    with pytest.raises(DevServiceError) as error:
        register_local_project("Relative", "relative/path")
    assert error.value.code == "PROJECT_PATH_NOT_ABSOLUTE"


def test_blank_name_and_path_are_rejected(isolated_registry, tmp_path):
    repo = make_git_repo(tmp_path / "repo-a")
    with pytest.raises(DevServiceError) as error:
        register_local_project("   ", str(repo))
    assert error.value.code == "PROJECT_NAME_REQUIRED"
    with pytest.raises(DevServiceError) as error:
        register_local_project("Repo A", "   ")
    assert error.value.code == "PROJECT_PATH_REQUIRED"


def test_duplicate_name_gets_a_unique_suffixed_id(isolated_registry, tmp_path):
    first = register_local_project("Same Name", str(make_git_repo(tmp_path / "repo-a")))
    second = register_local_project("Same Name", str(make_git_repo(tmp_path / "repo-b")))
    assert first["id"] != second["id"]
    assert second["id"].startswith(first["id"])


def test_endpoint_requires_loopback_and_exact_admin_token(
    isolated_registry, tmp_path, monkeypatch
):
    from nexus.web import routes

    monkeypatch.setenv("NEXUS_ADMIN_TOKEN", "x" * 32)
    repo = make_git_repo(tmp_path / "repo-a")
    body = routes.ProjectCreateRequest(name="Repo A", path=str(repo))

    def request(host, token=None):
        headers = [] if token is None else [(b"authorization", ("Bearer " + token).encode())]
        return Request({"type": "http", "method": "POST", "path": "/api/projects",
                        "client": (host, 50000), "headers": headers})

    with pytest.raises(HTTPException) as missing:
        routes.create_project(body, request("127.0.0.1"))
    assert missing.value.status_code == 401
    assert missing.value.detail["code"] == "ADMIN_AUTH_REQUIRED"

    with pytest.raises(HTTPException) as wrong:
        routes.create_project(body, request("127.0.0.1", "wrong-token-that-is-long-enough"))
    assert wrong.value.status_code == 401

    assert routes.create_project(body, request("127.0.0.1", "x" * 32))["name"] == "Repo A"

    other_repo = make_git_repo(tmp_path / "repo-b")
    other_body = routes.ProjectCreateRequest(name="Repo B", path=str(other_repo))
    with pytest.raises(HTTPException) as remote:
        routes.create_project(other_body, request("192.168.1.4", "x" * 32))
    assert remote.value.status_code == 403


def test_endpoint_without_admin_token_configured_is_rejected(
    isolated_registry, tmp_path, monkeypatch
):
    from nexus.web import routes

    monkeypatch.delenv("NEXUS_ADMIN_TOKEN", raising=False)
    repo = make_git_repo(tmp_path / "repo-a")
    body = routes.ProjectCreateRequest(name="Repo A", path=str(repo))
    request = Request({"type": "http", "method": "POST", "path": "/api/projects",
                        "client": ("127.0.0.1", 50000), "headers": []})

    with pytest.raises(HTTPException) as error:
        routes.create_project(body, request)
    assert error.value.status_code == 503
    assert error.value.detail["code"] == "ADMIN_AUTH_NOT_CONFIGURED"
