"""Adversarial regressions for the Windows worktree-deletion incident."""

import os
from pathlib import Path

import pytest

from nexus.worktree_safety import (
    NEXUS_SOURCE_REPOSITORY,
    UnsafeWorktreePathError,
    safe_rmtree,
    validate_destructive_target,
)


def _reject(root, candidate, **kwargs):
    with pytest.raises(UnsafeWorktreePathError):
        validate_destructive_target(root, candidate, **kwargs)


def test_valid_strict_descendant_is_accepted_and_temp_only_cleanup_works(tmp_path):
    container = tmp_path / "container"
    target = container / "worktree"
    target.mkdir(parents=True)
    (target / "proof.txt").write_text("temporary", encoding="utf-8")

    assert validate_destructive_target(container, target) == target.resolve()
    assert validate_destructive_target(
        str(container).replace("\\", "/"), str(target) + "\\"
    ) == target.resolve()
    safe_rmtree(container, target)

    assert container.is_dir()
    assert not target.exists()


def test_parent_sibling_same_prefix_and_container_are_rejected(tmp_path):
    container = tmp_path / "worktree"
    target = container / "child"
    sibling = tmp_path / "sibling"
    same_prefix = tmp_path / "worktree-evil"
    for path in (target, sibling, same_prefix):
        path.mkdir(parents=True)

    _reject(container, container)
    _reject(container, tmp_path)
    _reject(container, sibling)
    _reject(container, same_prefix)


def test_relative_empty_missing_and_parent_traversal_are_rejected(tmp_path):
    container = tmp_path / "container"
    target = container / "worktree"
    outside = tmp_path / "outside"
    target.mkdir(parents=True)
    outside.mkdir()

    _reject(container, "")
    _reject(container, Path("..") / "outside")
    _reject(container, target / ".." / ".." / "outside")
    _reject(container, container / "missing")


def test_protected_windows_roots_are_rejected_before_resolution(tmp_path, monkeypatch):
    container = tmp_path / "container"
    container.mkdir()
    fake_home = tmp_path / "profile"
    fake_home.mkdir()
    for name in ("Desktop", "Documents", "Downloads", "AppData"):
        (fake_home / name).mkdir()
    monkeypatch.setenv("USERPROFILE", str(fake_home))

    for protected in (
        "C:\\",
        r"C:\Users",
        fake_home,
        fake_home / "Desktop",
        fake_home / "Documents",
        fake_home / "Downloads",
        fake_home / "AppData",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")),
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")),
        Path(os.environ.get("ProgramData", r"C:\ProgramData")),
        Path(os.environ.get("WINDIR", r"C:\Windows")),
    ):
        _reject(container, protected)
    _reject(container, str(fake_home).upper())


def test_nexus_source_repository_is_protected():
    _reject(
        NEXUS_SOURCE_REPOSITORY.parent,
        NEXUS_SOURCE_REPOSITORY,
        protected_repo=NEXUS_SOURCE_REPOSITORY,
    )


def test_reparse_ambiguity_fails_closed(tmp_path, monkeypatch):
    container = tmp_path / "container"
    target = container / "worktree"
    target.mkdir(parents=True)

    monkeypatch.setattr(
        "nexus.worktree_safety._is_reparse_point",
        lambda path: Path(path) == target,
    )
    _reject(container, target)


def test_old_nested_cmd_rmdir_incident_cannot_reappear_in_cleanup_source():
    """Regression only inspects source; it never executes the old command."""
    integration_source = (
        NEXUS_SOURCE_REPOSITORY / "nexus" / "orchestration" / "integration.py"
    ).read_text(encoding="utf-8").lower()

    assert "cmd /c" not in integration_source
    assert "rmdir /s /q" not in integration_source
    assert "rd /s /q" not in integration_source
    assert "safe_rmtree" in integration_source
