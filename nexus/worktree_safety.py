"""Fail-closed boundary checks for Nexus-owned recursive deletion."""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path


class UnsafeWorktreePathError(ValueError):
    """Raised when a destructive target cannot be proven safe."""


NEXUS_SOURCE_REPOSITORY = Path(__file__).resolve().parents[1]


def _normalized_key(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def _same_path(left: Path, right: Path) -> bool:
    return _normalized_key(left) == _normalized_key(right)


def _is_reparse_point(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError as exc:
        raise UnsafeWorktreePathError(f"cannot inspect path component: {path}") from exc
    attributes = getattr(info, "st_file_attributes", 0)
    return stat.S_ISLNK(info.st_mode) or bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _protected_windows_paths() -> tuple[Path, ...]:
    system_drive = os.environ.get("SystemDrive", "C:")
    userprofile = os.environ.get("USERPROFILE")
    values = [
        Path(f"{system_drive}\\"),
        Path(f"{system_drive}\\Users"),
        Path(os.environ.get("ProgramFiles", f"{system_drive}\\Program Files")),
        Path(os.environ.get("ProgramFiles(x86)", f"{system_drive}\\Program Files (x86)")),
        Path(os.environ.get("ProgramData", f"{system_drive}\\ProgramData")),
        Path(os.environ.get("WINDIR", f"{system_drive}\\Windows")),
    ]
    if userprofile:
        home = Path(userprofile)
        values.extend(
            [
                home,
                home / "Desktop",
                home / "Documents",
                home / "Downloads",
                home / "AppData",
            ]
        )
    return tuple(values)


def validate_destructive_target(
    authorized_root: str | os.PathLike[str],
    candidate: str | os.PathLike[str],
    *,
    protected_repo: str | os.PathLike[str] = NEXUS_SOURCE_REPOSITORY,
) -> Path:
    """Return a proven-safe target or fail closed."""
    if authorized_root is None or candidate is None:
        raise UnsafeWorktreePathError("paths must not be empty")
    root_text = os.fspath(authorized_root)
    candidate_text = os.fspath(candidate)
    if not root_text.strip() or not candidate_text.strip():
        raise UnsafeWorktreePathError("paths must not be empty")

    root_input = Path(root_text)
    candidate_input = Path(candidate_text)
    if not root_input.is_absolute() or not candidate_input.is_absolute():
        raise UnsafeWorktreePathError("paths must be absolute")
    if ".." in root_input.parts or ".." in candidate_input.parts:
        raise UnsafeWorktreePathError("parent traversal is not allowed")

    for protected in _protected_windows_paths():
        if _same_path(candidate_input, protected):
            raise UnsafeWorktreePathError(f"protected Windows path: {candidate_input}")

    # Inspect the lexical path before resolve() erases evidence of a junction
    # or symlink. Every component from the candidate back to the authorized
    # root must be directly inspectable.
    cursor = candidate_input
    while True:
        if _is_reparse_point(cursor):
            raise UnsafeWorktreePathError("target crosses a reparse point")
        if _same_path(cursor, root_input):
            break
        parent = cursor.parent
        if parent == cursor:
            break
        cursor = parent

    try:
        root = root_input.resolve(strict=True)
        target = candidate_input.resolve(strict=True)
    except OSError as exc:
        raise UnsafeWorktreePathError("path resolution is ambiguous or missing") from exc

    if not root.is_dir() or not target.is_dir():
        raise UnsafeWorktreePathError("root and target must be existing directories")
    for protected in _protected_windows_paths():
        try:
            resolved_protected = protected.resolve(strict=True)
        except OSError:
            continue
        if _same_path(target, resolved_protected):
            raise UnsafeWorktreePathError(f"protected Windows path: {target}")
    if target == Path(target.anchor):
        raise UnsafeWorktreePathError("filesystem roots are never destructive targets")
    if _same_path(root, target):
        raise UnsafeWorktreePathError("target must be a strict descendant")
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise UnsafeWorktreePathError("target escapes the authorized root") from exc
    if not relative.parts:
        raise UnsafeWorktreePathError("target must be a strict descendant")

    try:
        protected_source = Path(protected_repo).resolve(strict=True)
    except OSError as exc:
        raise UnsafeWorktreePathError("protected repository cannot be resolved") from exc
    try:
        protected_source.relative_to(target)
    except ValueError:
        pass
    else:
        raise UnsafeWorktreePathError("target contains the Nexus source repository")

    return target


def safe_rmtree(
    authorized_root: str | os.PathLike[str],
    candidate: str | os.PathLike[str],
    *,
    protected_repo: str | os.PathLike[str] = NEXUS_SOURCE_REPOSITORY,
) -> None:
    """Recursively delete only after deterministic boundary validation."""
    target = validate_destructive_target(
        authorized_root, candidate, protected_repo=protected_repo
    )
    shutil.rmtree(target)
