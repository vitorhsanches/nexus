"""Tests for nexus.dev.worktree -- F1.1: isolated development worktrees.

Scope matches the module under test exactly: creation, identification,
validation and safe removal of one worktree per session. No agent
execution, no Chat, no UI, no branch integration is exercised here.

Every test uses disposable, temporary Git repositories (pytest's own
tmp_path) and points the module's dedicated worktree root at another
temporary directory via NEXUS_DEV_WORKTREE_ROOT, so nothing here ever
touches the real Nexus repository or real Nexus-owned state.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from nexus.dev.worktree import (
    DevWorktreeError,
    ForeignWorktreeError,
    InvalidProjectError,
    InvalidSessionIdError,
    WorktreeCollisionError,
    WorktreeNotCleanError,
    branch_name_for_session,
    create_dev_worktree,
    read_worktree_changed_files,
    read_worktree_diff,
    remove_dev_worktree,
)


@pytest.fixture(autouse=True)
def isolated_worktree_root(tmp_path, monkeypatch):
    """Point the module's dedicated worktree root at a disposable
    directory for every test in this file -- never the real
    %LOCALAPPDATA%\\Nexus\\DevWorktrees location."""
    root = tmp_path / "dev-worktree-root"
    monkeypatch.setenv("NEXUS_DEV_WORKTREE_ROOT", str(root))
    return root


def _run(args, cwd):
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def _init_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    _run(["init", "-q"], cwd=str(path))
    _run(["config", "user.email", "dev-worktree-test@example.com"], cwd=str(path))
    _run(["config", "user.name", "dev-worktree-test"], cwd=str(path))
    (path / "README.md").write_text("placeholder\n", encoding="utf-8")
    _run(["add", "-A"], cwd=str(path))
    _run(["commit", "-q", "-m", "init"], cwd=str(path))
    return _run(["rev-parse", "HEAD"], cwd=str(path))


def _project(tmp_path) -> Path:
    repo = tmp_path / "project"
    _init_repo(repo)
    return Path(os.path.realpath(str(repo)))


# -- 1 & 2: correct creation, expected branch and SHA ----------------------


def test_create_dev_worktree_returns_an_isolated_worktree_on_the_expected_branch_and_sha(tmp_path):
    project = _project(tmp_path)
    head_sha = _run(["rev-parse", "HEAD"], cwd=str(project))

    worktree = create_dev_worktree(str(project), "session-one")

    assert os.path.isdir(worktree)
    assert _run(["rev-parse", "HEAD"], cwd=worktree) == head_sha
    assert _run(["rev-parse", "--abbrev-ref", "HEAD"], cwd=worktree) == "nexus/dev/session-one"
    assert branch_name_for_session("session-one") == "nexus/dev/session-one"
    # The branch must actually be registered in the project repo.
    branches = _run(["branch", "--list", "nexus/dev/session-one"], cwd=str(project))
    assert "nexus/dev/session-one" in branches


# -- 3: isolated from the original project directory ------------------------


def test_worktree_directory_is_isolated_from_the_original_project(tmp_path):
    project = _project(tmp_path)
    worktree = create_dev_worktree(str(project), "session-iso")

    worktree_path = Path(os.path.realpath(worktree))
    assert worktree_path != project
    assert project not in worktree_path.parents
    assert worktree_path not in project.parents


# -- 4: changes inside the worktree never touch the main project ------------


def test_changes_inside_the_worktree_never_modify_the_main_project(tmp_path):
    project = _project(tmp_path)
    worktree = create_dev_worktree(str(project), "session-write")

    (Path(worktree) / "new_file.txt").write_text("created inside the worktree only\n", encoding="utf-8")
    (Path(project) / "README.md").read_text(encoding="utf-8")  # still readable/unchanged below

    assert not (project / "new_file.txt").exists()
    assert (project / "README.md").read_text(encoding="utf-8") == "placeholder\n"
    status = _run(["status", "--porcelain"], cwd=str(project))
    assert status == "", f"main project working tree must stay clean, got: {status!r}"


# -- 5: invalid repository ---------------------------------------------------


def test_create_rejects_a_project_subdirectory_that_is_not_a_git_repository_root(tmp_path):
    # tmp_path can itself live inside the user's Git repository. A nested
    # directory is not an independent project root, even though Git reports
    # that it is inside a worktree.
    project = _project(tmp_path)
    not_a_repo = project / "plain_dir"
    not_a_repo.mkdir()
    with pytest.raises(InvalidProjectError):
        create_dev_worktree(str(not_a_repo), "session-x")


def test_create_rejects_a_project_path_that_does_not_exist(tmp_path):
    with pytest.raises(InvalidProjectError):
        create_dev_worktree(str(tmp_path / "does_not_exist"), "session-x")


# -- 6: invalid session_id ---------------------------------------------------


@pytest.mark.parametrize(
    "bad_session_id",
    [
        "",
        "../escape",
        "has space",
        "has/slash",
        "has\\backslash",
        ".",
        "..",
        "a" * 65,  # over the length bound
        "-leading-dash",
        "ok but not quite; semicolon",
    ],
)
def test_create_rejects_invalid_session_ids(tmp_path, bad_session_id):
    project = _project(tmp_path)
    with pytest.raises(InvalidSessionIdError):
        create_dev_worktree(str(project), bad_session_id)


def test_remove_also_rejects_invalid_session_ids(tmp_path):
    project = _project(tmp_path)
    with pytest.raises(InvalidSessionIdError):
        remove_dev_worktree(str(project), str(tmp_path / "whatever"), "bad id with spaces")


# -- 7: directory and branch collisions --------------------------------------


def test_create_rejects_a_directory_collision(tmp_path):
    project = _project(tmp_path)
    create_dev_worktree(str(project), "session-dup")
    with pytest.raises(WorktreeCollisionError):
        create_dev_worktree(str(project), "session-dup")


def test_create_rejects_a_branch_collision_even_without_a_directory_collision(tmp_path):
    project = _project(tmp_path)
    # Pre-create only the branch (no worktree directory at all) to isolate
    # the branch-collision check from the directory-collision check.
    _run(["branch", "nexus/dev/session-branch-only"], cwd=str(project))
    with pytest.raises(WorktreeCollisionError):
        create_dev_worktree(str(project), "session-branch-only")


# -- 8: refuse removal with unsaved changes ----------------------------------


def test_remove_refuses_a_worktree_with_tracked_changes(tmp_path):
    project = _project(tmp_path)
    worktree = create_dev_worktree(str(project), "session-dirty-tracked")
    (Path(worktree) / "README.md").write_text("modified\n", encoding="utf-8")

    with pytest.raises(WorktreeNotCleanError):
        remove_dev_worktree(str(project), worktree, "session-dirty-tracked")
    assert os.path.isdir(worktree), "a refused removal must leave the worktree in place"


def test_remove_refuses_a_worktree_with_untracked_files(tmp_path):
    project = _project(tmp_path)
    worktree = create_dev_worktree(str(project), "session-dirty-untracked")
    (Path(worktree) / "scratch.txt").write_text("untracked\n", encoding="utf-8")

    with pytest.raises(WorktreeNotCleanError):
        remove_dev_worktree(str(project), worktree, "session-dirty-untracked")
    assert os.path.isdir(worktree)


# -- 9: refuse removal of another session's worktree -------------------------


def test_remove_refuses_a_worktree_belonging_to_a_different_session(tmp_path):
    project = _project(tmp_path)
    worktree_a = create_dev_worktree(str(project), "session-a")
    create_dev_worktree(str(project), "session-b")

    with pytest.raises(ForeignWorktreeError):
        remove_dev_worktree(str(project), worktree_a, "session-b")
    assert os.path.isdir(worktree_a), "the mismatched call must not remove anything"


def test_remove_refuses_a_path_outside_the_dedicated_worktree_root(tmp_path):
    project = _project(tmp_path)
    create_dev_worktree(str(project), "session-root-escape")
    outside = tmp_path / "not_in_worktree_root"
    outside.mkdir()

    with pytest.raises(DevWorktreeError):
        remove_dev_worktree(str(project), str(outside), "session-root-escape")
    assert outside.exists(), "a path outside the authorized root must never be touched"


# -- 10: safe removal of a clean worktree ------------------------------------


def test_remove_safely_removes_a_clean_worktree_and_its_branch(tmp_path):
    project = _project(tmp_path)
    worktree = create_dev_worktree(str(project), "session-clean")

    remove_dev_worktree(str(project), worktree, "session-clean")

    assert not os.path.exists(worktree)
    branches = _run(["branch", "--list", "nexus/dev/session-clean"], cwd=str(project))
    assert branches == "", "the session branch must be deleted once the worktree is gone"
    registered = _run(["worktree", "list", "--porcelain"], cwd=str(project))
    assert os.path.normcase(worktree) not in os.path.normcase(registered)


# -- 11: idempotent removal ---------------------------------------------------


def test_remove_is_idempotent_once_the_sessions_resources_are_already_gone(tmp_path):
    project = _project(tmp_path)
    worktree = create_dev_worktree(str(project), "session-idempotent")
    remove_dev_worktree(str(project), worktree, "session-idempotent")

    # Calling it again (same, now-absent path) must not raise.
    remove_dev_worktree(str(project), worktree, "session-idempotent")


def test_remove_is_idempotent_for_a_session_that_was_never_created(tmp_path, isolated_worktree_root):
    project = _project(tmp_path)
    never_created = str(isolated_worktree_root / "session-never-existed")
    # Never created at all -- still must be a safe no-op, not an error.
    remove_dev_worktree(str(project), never_created, "session-never-existed")


# -- 12: safety under partial failure ----------------------------------------


def test_a_rejected_creation_leaves_no_partial_worktree_directory_behind(tmp_path, isolated_worktree_root):
    project = _project(tmp_path)
    # Simulate pre-existing partial state: the branch exists (e.g. from a
    # previous, differently-failed attempt) but no worktree directory does.
    _run(["branch", "nexus/dev/session-partial"], cwd=str(project))

    with pytest.raises(WorktreeCollisionError):
        create_dev_worktree(str(project), "session-partial")

    # The failed call must not have created a worktree directory, must not
    # have touched the project's own working tree, and the pre-existing
    # branch must be exactly as it was (still present, not corrupted).
    assert not (isolated_worktree_root / "session-partial").exists()
    assert _run(["status", "--porcelain"], cwd=str(project)) == ""
    branches = _run(["branch", "--list", "nexus/dev/session-partial"], cwd=str(project))
    assert "nexus/dev/session-partial" in branches


def test_create_failure_never_deletes_preexisting_unrelated_data(tmp_path):
    project = _project(tmp_path)
    # An unrelated worktree already exists at session "keep-me".
    kept = create_dev_worktree(str(project), "keep-me")
    (Path(project) / "untouched_marker.txt").write_text("must survive\n", encoding="utf-8")
    _run(["add", "-A"], cwd=str(project))
    _run(["commit", "-q", "-m", "marker"], cwd=str(project))

    # A second, colliding attempt on a DIFFERENT, already-used session_id.
    with pytest.raises(WorktreeCollisionError):
        create_dev_worktree(str(project), "keep-me")

    assert os.path.isdir(kept), "pre-existing worktree data must survive an unrelated failure"
    assert (project / "untouched_marker.txt").exists()


# -- read_worktree_changed_files (F2.4A): read-only Git status summary -----


def test_changed_files_is_empty_for_a_freshly_created_worktree(tmp_path):
    project = _project(tmp_path)
    worktree = create_dev_worktree(str(project), "session-clean")
    assert read_worktree_changed_files(worktree) == []


def test_changed_files_reports_added_modified_and_deleted_paths(tmp_path):
    project = _project(tmp_path)
    worktree = create_dev_worktree(str(project), "session-mixed")

    (Path(worktree) / "README.md").write_text("changed content\n", encoding="utf-8")
    (Path(worktree) / "new_file.py").write_text("def f(): pass\n", encoding="utf-8")
    os.remove(Path(worktree) / ".gitignore") if (Path(worktree) / ".gitignore").exists() else None

    files = read_worktree_changed_files(worktree)
    by_path = {entry["path"]: entry["status"] for entry in files}
    assert by_path.get("README.md") == "modified"
    assert by_path.get("new_file.py") == "added"
    # Sorted by path, deterministic order for a stable UI render.
    assert [entry["path"] for entry in files] == sorted(entry["path"] for entry in files)


def test_changed_files_reports_a_deleted_tracked_file(tmp_path):
    project = _project(tmp_path)
    worktree = create_dev_worktree(str(project), "session-deleted")
    os.remove(Path(worktree) / "README.md")
    files = read_worktree_changed_files(worktree)
    assert files == [{"path": "README.md", "status": "deleted"}]


def test_changed_files_reports_a_renamed_file_without_leaking_the_old_path_as_its_own_entry(tmp_path):
    project = _project(tmp_path)
    worktree = create_dev_worktree(str(project), "session-renamed")
    _run(["mv", "README.md", "RENAMED.md"], cwd=worktree)
    files = read_worktree_changed_files(worktree)
    assert files == [{"path": "RENAMED.md", "status": "renamed"}]


def test_review_diff_includes_untracked_and_unicode_paths_without_staging(tmp_path):
    project = _project(tmp_path)
    worktree = create_dev_worktree(str(project), "diff-untracked")
    new_file = Path(worktree) / "新しい file.txt"
    new_file.write_text("untracked line\n", encoding="utf-8")
    review = read_worktree_diff(worktree, _run(["rev-parse", "HEAD"], cwd=worktree))
    entry = next(item for item in review["files"] if item["path"] == "新しい file.txt")
    assert entry["status"] == "untracked"
    assert "+untracked line" in entry["patch"]
    assert _run(["diff", "--cached", "--name-only"], cwd=worktree) == ""


def test_review_diff_includes_staged_and_unstaged_changes_and_renames(tmp_path):
    project = _project(tmp_path)
    (project / "rename-source.txt").write_text("same content stays intact\n", encoding="utf-8")
    _run(["add", "rename-source.txt"], cwd=str(project))
    _run(["commit", "-q", "-m", "rename fixture"], cwd=str(project))
    worktree = create_dev_worktree(str(project), "diff-staged")
    readme = Path(worktree) / "README.md"
    readme.write_text("staged version\n", encoding="utf-8")
    _run(["add", "README.md"], cwd=worktree)
    readme.write_text("unstaged version\n", encoding="utf-8")
    staged_added = Path(worktree) / "staged-added.py"
    staged_added.write_text("staged addition\n", encoding="utf-8")
    _run(["add", "staged-added.py"], cwd=worktree)
    _run(["mv", "rename-source.txt", "RENAMED.txt"], cwd=worktree)
    review = read_worktree_diff(worktree, _run(["rev-parse", "HEAD"], cwd=worktree))
    by_path = {item["path"]: item for item in review["files"]}
    assert "RENAMED.txt" in by_path and by_path["RENAMED.txt"]["status"] == "renamed"
    assert "README.md" in by_path and by_path["README.md"]["status"] == "modified"
    assert "staged-added.py" in by_path and by_path["staged-added.py"]["status"] == "added"
    # Regression coverage (F3.1 live smoke finding): a modified/staged
    # TRACKED file's actual patch content, not just its status label, must
    # be present. The combination of the global --literal-pathspecs flag
    # with an explicit ":(literal)" pathspec-magic prefix on the same path
    # silently cancels out (git finds no match and returns an empty diff,
    # exit 0, no error) -- status labels still came from `git diff
    # --name-status`, a separate call unaffected by the bug, so this kind
    # of regression is invisible unless patch content is actually checked.
    readme_entry = by_path["README.md"]
    assert readme_entry["patch"], "modified tracked file must have real patch content, not an empty string"
    assert "+unstaged version" in readme_entry["patch"]
    assert readme_entry["additions"] == 1 and readme_entry["deletions"] == 1
    staged_entry = by_path["staged-added.py"]
    assert staged_entry["patch"], "staged-added tracked file must have real patch content"
    assert "+staged addition" in staged_entry["patch"]


def test_review_diff_reports_deleted_and_sensitive_files_without_exposing_content(tmp_path):
    project = _project(tmp_path)
    (project / "delete-me.txt").write_text("old tracked content\n", encoding="utf-8")
    _run(["add", "delete-me.txt"], cwd=str(project))
    _run(["commit", "-q", "-m", "add deletion fixture"], cwd=str(project))
    worktree = create_dev_worktree(str(project), "diff-deleted")
    os.remove(Path(worktree) / "delete-me.txt")
    env_file = Path(worktree) / ".env"
    env_file.write_text("API_TOKEN=must-not-leak\n", encoding="utf-8")
    review = read_worktree_diff(worktree, _run(["rev-parse", "HEAD"], cwd=worktree))
    by_path = {item["path"]: item for item in review["files"]}
    assert by_path["delete-me.txt"]["status"] == "deleted"
    assert by_path["delete-me.txt"]["patch"], "a deleted tracked file must have real patch content"
    assert "-old tracked content" in by_path["delete-me.txt"]["patch"]
    assert by_path[".env"]["content_omitted"] is True
    assert "must-not-leak" not in str(review)


def test_review_diff_omits_large_patches_and_marks_response_truncated(tmp_path):
    project = _project(tmp_path)
    worktree = create_dev_worktree(str(project), "diff-large")
    (Path(worktree) / "large.txt").write_text("x" * (128 * 1024 + 1), encoding="utf-8")
    review = read_worktree_diff(worktree, _run(["rev-parse", "HEAD"], cwd=worktree))
    large = next(item for item in review["files"] if item["path"] == "large.txt")
    assert large["truncated"] is True
    assert large["patch"] is None
    assert review["truncated"] is True


def test_review_diff_marks_binary_and_omits_symlink_content(tmp_path):
    project = _project(tmp_path)
    worktree = create_dev_worktree(str(project), "diff-binary")
    Path(worktree, "image.bin").write_bytes(b"\x00\x01\x02")
    target = tmp_path / "outside-secret.txt"
    target.write_text("secret outside\n", encoding="utf-8")
    try:
        Path(worktree, "outside-link.txt").symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this platform")
    review = read_worktree_diff(worktree, _run(["rev-parse", "HEAD"], cwd=worktree))
    by_path = {item["path"]: item for item in review["files"]}
    assert by_path["image.bin"]["binary"] is True
    assert by_path["image.bin"]["patch"] is None
    assert by_path["outside-link.txt"]["patch"] is None
    assert "secret outside" not in str(review)


def test_review_diff_rejects_invalid_base_sha_and_is_stable_when_clean(tmp_path):
    project = _project(tmp_path)
    worktree = create_dev_worktree(str(project), "diff-clean")
    base = _run(["rev-parse", "HEAD"], cwd=worktree)
    first = read_worktree_diff(worktree, base)
    second = read_worktree_diff(worktree, base)
    assert first == second
    assert first["files"] == []
    with pytest.raises(DevWorktreeError):
        read_worktree_diff(worktree, "not-a-commit")


def test_changed_files_never_reflects_changes_made_only_in_the_original_project(tmp_path):
    """Worktree isolation, from the read side: editing the ORIGINAL
    project's own working tree must never appear in a session's changed
    files -- they are different working directories sharing one repo."""
    project = _project(tmp_path)
    worktree = create_dev_worktree(str(project), "session-iso-read")
    (project / "README.md").write_text("edited in the original project\n", encoding="utf-8")
    assert read_worktree_changed_files(worktree) == []
