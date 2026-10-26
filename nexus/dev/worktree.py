"""Isolated Git worktree lifecycle for Nexus Development Mode sessions.

Scope (F1.1 -- strictly this and nothing else): create, identify, validate
and safely remove ONE development worktree per session. No agent
execution, no write-capable Chat, no UI, no branch integration into the
project's main branch. Those are later phases built on top of this.

Design notes:

* Every git invocation uses an argument list via subprocess.run, never a
  shell string -- see _run_git().
* Worktrees live under a dedicated, Nexus-owned root
  (dev_worktree_root()), never inside the project repository and never
  inside another worktree. The root itself follows the same
  override-chain convention nexus/process_env.py already uses for
  CODEX_HOME (NEXUS_DEV_WORKTREE_ROOT env override, else LocalAppData,
  else a per-user home directory, else the OS temp directory), so tests
  can point it at a disposable location without touching real state.
* Removal never uses `git worktree remove --force` and never runs
  `git worktree prune`. A dirty worktree (tracked changes OR untracked
  files) is refused, not overridden. Forced discard is an explicit,
  separate, future capability -- not implemented here.
* On any failure during creation, nothing this module may have already
  created is deleted automatically -- the caller gets a clear, typed
  error and the partially-created state (if any) is left for manual or
  later, explicitly-authorized cleanup. This trades a small amount of
  disk hygiene for the much stronger guarantee of never deleting data
  on a code path that was only ever supposed to create it.
"""

from __future__ import annotations

import os
import re
import difflib
import hashlib
import json
import stat
import subprocess
import tempfile
import uuid
from pathlib import Path

from nexus.worktree_safety import UnsafeWorktreePathError, validate_destructive_target

__all__ = [
    "DevWorktreeError",
    "InvalidSessionIdError",
    "InvalidProjectError",
    "WorktreeCollisionError",
    "WorktreeNotCleanError",
    "ForeignWorktreeError",
    "dev_worktree_root",
    "branch_name_for_session",
    "read_worktree_head_sha",
    "read_worktree_diff",
    "verify_dev_worktree",
    "create_dev_worktree",
    "remove_dev_worktree",
]


class DevWorktreeError(RuntimeError):
    """Base error for all nexus.dev.worktree failures."""


class InvalidSessionIdError(DevWorktreeError, ValueError):
    """session_id failed the explicit, restrictive validation rule."""


class InvalidProjectError(DevWorktreeError, ValueError):
    """project_path is not a usable, existing Git repository."""


class WorktreeCollisionError(DevWorktreeError):
    """The target directory or branch name is already in use."""


class WorktreeNotCleanError(DevWorktreeError):
    """Refused: the worktree has tracked changes or untracked files."""


class ForeignWorktreeError(DevWorktreeError):
    """Refused: the path is not this project's worktree for this session."""


# Alphanumeric start, alphanumeric/dash/underscore body, bounded length.
# Deliberately excludes '.', '/', '\\', spaces and any non-ASCII byte so a
# session_id can never be used to escape the dedicated worktree directory,
# collide with "." / "..", or inject anything into a git branch name/ref.
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

_BRANCH_PREFIX = "nexus/dev/"


def _validate_session_id(session_id: str) -> str:
    if not isinstance(session_id, str) or not _SESSION_ID_RE.match(session_id):
        raise InvalidSessionIdError(
            "session_id must match ^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$ "
            f"(got {session_id!r})"
        )
    return session_id


def branch_name_for_session(session_id: str) -> str:
    """Return the dedicated branch name for a session, after validation."""
    return _BRANCH_PREFIX + _validate_session_id(session_id)


def _run_git(args: list[str], *, cwd: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run one git subcommand via an argument list -- never a shell string."""
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and result.returncode != 0:
        raise DevWorktreeError(
            "git " + " ".join(args[:6]) + f" failed (exit {result.returncode}) in {cwd}: "
            + result.stderr.strip()[-800:]
        )
    return result


def dev_worktree_root(env: dict | None = None) -> Path:
    """Return the dedicated, Nexus-owned root dev worktrees live under.

    Same override chain as nexus.process_env's CODEX_HOME resolution:
    an explicit env override first (NEXUS_DEV_WORKTREE_ROOT, primarily for
    test isolation), then LocalAppData, then a per-user home directory,
    then the OS temp directory. Never inside the project repository this
    function knows nothing about, and never the shared ~/.codex-style
    location. The directory is created if missing.
    """
    source = env if env is not None else os.environ
    override = source.get("NEXUS_DEV_WORKTREE_ROOT")
    if override:
        root = Path(override)
    else:
        localappdata = source.get("LOCALAPPDATA")
        userprofile = source.get("USERPROFILE") or source.get("HOME")
        if localappdata:
            root = Path(localappdata) / "Nexus" / "DevWorktrees"
        elif userprofile:
            root = Path(userprofile) / ".nexus_dev_worktrees"
        else:
            root = Path(tempfile.gettempdir()) / "Nexus" / "DevWorktrees"
    root = Path(os.path.realpath(str(root)))
    root.mkdir(parents=True, exist_ok=True)
    return root


def _require_git_repo(project_path: str) -> Path:
    if not isinstance(project_path, str) or not project_path.strip():
        raise InvalidProjectError("project_path must not be empty")
    candidate = Path(project_path)
    if not candidate.is_absolute():
        raise InvalidProjectError(f"project_path must be absolute: {project_path!r}")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise InvalidProjectError(f"project_path does not exist: {project_path!r}") from exc
    if not resolved.is_dir():
        raise InvalidProjectError(f"project_path is not a directory: {project_path!r}")
    probe = _run_git(["rev-parse", "--show-toplevel"], cwd=str(resolved), check=False)
    if probe.returncode != 0 or not probe.stdout.strip():
        raise InvalidProjectError(f"project_path is not a Git repository: {project_path!r}")
    try:
        git_root = Path(probe.stdout.strip()).resolve(strict=True)
    except OSError as exc:
        raise InvalidProjectError(f"project_path is not a usable Git repository: {project_path!r}") from exc
    if os.path.normcase(str(git_root)) != os.path.normcase(str(resolved)):
        raise InvalidProjectError(
            f"project_path must be the Git repository root: {project_path!r} (root: {str(git_root)!r})"
        )
    return resolved


def _head_sha(repo: Path) -> str:
    result = _run_git(["rev-parse", "HEAD"], cwd=str(repo))
    sha = result.stdout.strip()
    if not sha:
        raise DevWorktreeError(f"could not determine HEAD for {repo}")
    return sha


def _git_common_dir(path: Path) -> Path:
    result = _run_git(["rev-parse", "--git-common-dir"], cwd=str(path))
    raw = result.stdout.strip()
    common = Path(raw)
    if not common.is_absolute():
        common = (path / common).resolve()
    else:
        common = common.resolve()
    return common


def _branch_exists(repo: Path, branch: str) -> bool:
    result = _run_git(["branch", "--list", branch], cwd=str(repo))
    return bool(result.stdout.strip())


def read_worktree_head_sha(worktree_path: str) -> str:
    """Return a worktree's current HEAD SHA.

    Read-only; a small public helper so callers (e.g. nexus/dev/service.py,
    which needs to persist a session's base_sha) never have to reach into
    this module's private git-invocation helper directly.
    """
    return _head_sha(Path(worktree_path))


# Porcelain v1 status codes (first two bytes of each line) mapped to a
# short, UI-stable label. "??" (untracked) is included -- a brand-new file
# the agent created is exactly the kind of change a reviewer most needs to
# see, not a lesser case to filter out.
_STATUS_LABELS = {
    "??": "added", "A ": "added", " A": "added", "AM": "added",
    "M ": "modified", " M": "modified", "MM": "modified",
    "D ": "deleted", " D": "deleted",
    "R ": "renamed", " R": "renamed",
    "C ": "copied", " C": "copied",
}


def read_worktree_changed_files(worktree_path: str) -> list[dict]:
    """Read-only summary of what differs in the worktree from its own
    last commit (i.e. from the session's base_sha, since F2.4A never lets
    the agent commit) -- staged, unstaged and untracked changes alike.

    Returns a list of ``{"path": <repo-relative path>, "status": <label>}``
    dicts, sorted by path, bounded to a sane count so a runaway agent
    (or a worktree the user ran something unrelated in) can never make
    this response unbounded. Purely informational: never used to decide
    whether a turn may run, and never touches history, staging, or the
    working tree itself.
    """
    repo = Path(worktree_path)
    result = _run_git(["status", "--porcelain=v1", "--untracked-files=all", "-z"], cwd=str(repo))
    raw = result.stdout
    entries: list[dict] = []
    i = 0
    while i < len(raw):
        # Each entry is "XY <path>\0", and a rename/copy entry is
        # immediately followed by a second NUL-terminated "<old-path>\0"
        # that status (not diff) still emits even in -z mode -- skip it
        # rather than misread it as its own unrelated entry.
        end = raw.find("\0", i)
        if end == -1:
            break
        record = raw[i:end]
        i = end + 1
        if len(record) < 4:
            continue
        code = record[:2]
        path = record[3:]
        label = _STATUS_LABELS.get(code, "changed")
        entries.append({"path": path, "status": label})
        if code[0] in ("R", "C"):
            # Skip the old-path record that follows a rename/copy.
            next_end = raw.find("\0", i)
            if next_end != -1:
                i = next_end + 1
    entries.sort(key=lambda item: item["path"])
    return entries[:500]


_DIFF_MAX_FILES = 80
_DIFF_MAX_FILE_BYTES = 128 * 1024
_DIFF_MAX_TOTAL_BYTES = 1024 * 1024
_BASE_SHA_RE = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")


def _sensitive_review_path(path: str) -> bool:
    """Conservatively omit content commonly containing credentials/config."""
    normalized = path.replace("\\", "/").lower()
    name = normalized.rsplit("/", 1)[-1]
    parts = set(normalized.split("/"))
    return (
        name == "auth.json" or name == ".env" or name.startswith(".env.")
        or name in {"id_rsa", "id_ed25519", "credentials", "secrets.json", "config.toml", ".npmrc", ".pypirc", ".netrc", ".git-credentials"}
        or name.endswith((".pem", ".p12", ".pfx", ".key"))
        or bool(parts & {".aws", ".ssh", ".codex"})
        or normalized.startswith("nexus/storage/")
    )


def read_worktree_diff(worktree_path: str, base_sha: str) -> dict:
    """Return a bounded, read-only review of a worktree against its saved base.

    Paths are obtained from Git, never from the caller. Tracked content is
    produced by read-only ``git diff``; untracked text is rendered in memory
    and never staged. Sensitive, binary, symlink, and oversized content is
    represented by metadata only.
    """
    if not isinstance(base_sha, str) or not _BASE_SHA_RE.fullmatch(base_sha):
        raise DevWorktreeError("The saved base commit is invalid; diff unavailable.")
    repo = Path(worktree_path)
    base_check = _run_git(["rev-parse", "--verify", "--end-of-options", f"{base_sha}^{{commit}}"], cwd=str(repo), check=False)
    if base_check.returncode != 0 or base_check.stdout.strip().lower() != base_sha.lower():
        raise DevWorktreeError("The saved base commit is unavailable in this worktree.")
    head_sha = read_worktree_head_sha(str(repo))

    raw_names = _run_git(["diff", "--no-ext-diff", "--no-textconv", "--name-status", "-z", "--find-renames", base_sha, "--"], cwd=str(repo)).stdout
    tokens = raw_names.split("\0")
    tracked: list[dict] = []
    i = 0
    labels = {"A": "added", "M": "modified", "D": "deleted", "R": "renamed", "C": "copied", "T": "modified"}
    while i < len(tokens) and tokens[i]:
        code = tokens[i]
        i += 1
        if code.startswith(("R", "C")) and i + 1 < len(tokens):
            old_path, path = tokens[i], tokens[i + 1]
            i += 2
            tracked.append({"path": path, "old_path": old_path, "status": labels[code[0]]})
        elif i < len(tokens):
            path = tokens[i]
            i += 1
            tracked.append({"path": path, "status": labels.get(code[:1], "modified")})

    untracked_raw = _run_git(["ls-files", "--others", "--exclude-standard", "-z"], cwd=str(repo)).stdout
    untracked = [{"path": p, "status": "untracked"} for p in untracked_raw.split("\0") if p]
    entries = sorted(tracked + untracked, key=lambda row: row["path"])
    truncated = len(entries) > _DIFF_MAX_FILES
    entries = entries[:_DIFF_MAX_FILES]
    total_bytes = 0
    files: list[dict] = []

    for entry in entries:
        path = entry["path"]
        patch = None
        item = {**entry, "additions": None, "deletions": None, "binary": False,
                "patch": None, "content_omitted": False, "truncated": False, "reason": None}
        candidate = repo / Path(path)
        # Refuse to traverse any symlink component, including a parent
        # directory. Git paths are repo-relative, but the worktree itself
        # may contain a malicious symlink inserted after session creation.
        current = repo
        unsafe_link = False
        try:
            components = Path(path).parts
            for position, component in enumerate(components):
                current = current / component
                component_info = current.lstat()
                if stat.S_ISLNK(component_info.st_mode):
                    unsafe_link = True
                    info = component_info if position == len(components) - 1 else None
                    break
            else:
                info = current.lstat()
        except OSError:
            info = None
        if info is not None and stat.S_ISREG(info.st_mode) and info.st_size <= _DIFF_MAX_FILE_BYTES:
            item["content_sha256"] = hashlib.sha256(candidate.read_bytes()).hexdigest()
            item["file_type"] = "regular"
            item["mode"] = 0o100755 if info.st_mode & stat.S_IXUSR else 0o100644
        elif info is not None and stat.S_ISLNK(info.st_mode):
            item["content_sha256"] = hashlib.sha256(os.fsencode(os.readlink(candidate))).hexdigest()
            item["file_type"] = "symlink"
            item["mode"] = 0o120000
        if _sensitive_review_path(path):
            item.update(content_omitted=True, reason="Sensitive file content is omitted.")
        elif unsafe_link or (info is not None and stat.S_ISLNK(info.st_mode)):
            item.update(content_omitted=True, reason="Symlink content is omitted for safety.")
        elif info is not None and not stat.S_ISREG(info.st_mode):
            item.update(content_omitted=True, reason="Non-regular file content is omitted.")
        elif info is not None and stat.S_ISREG(info.st_mode) and info.st_size > _DIFF_MAX_FILE_BYTES:
            item.update(truncated=True, reason="File exceeds the per-file review limit.")
            truncated = True
        elif entry["status"] == "untracked":
            try:
                raw = candidate.read_bytes()
                text = raw.decode("utf-8")
                if b"\0" in raw:
                    raise UnicodeError
                item["content_sha256"] = hashlib.sha256(raw).hexdigest()
                item["file_type"] = "text"
                item["mode"] = 0o100755 if info and info.st_mode & stat.S_IXUSR else 0o100644
                lines = text.splitlines(keepends=True)
                # Keep the path only in structured metadata. A filename may
                # contain newlines and must not be able to forge patch headers.
                patch = "".join(difflib.unified_diff([], lines, fromfile="/dev/null", tofile="b/untracked", n=3))
                item["additions"] = len(lines)
                item["deletions"] = 0
            except (OSError, UnicodeError):
                item.update(binary=True, content_omitted=True, reason="Binary or non-UTF-8 file content is omitted.")
                patch = None
        else:
            if info is not None and stat.S_ISLNK(info.st_mode):
                link_bytes = os.fsencode(os.readlink(candidate))
                item["content_sha256"] = hashlib.sha256(link_bytes).hexdigest()
                item["file_type"] = "symlink"
                item["mode"] = 0o120000
            elif info is not None and stat.S_ISREG(info.st_mode):
                content_hash = hashlib.sha256()
                with candidate.open("rb") as source:
                    for chunk in iter(lambda: source.read(64 * 1024), b""):
                        content_hash.update(chunk)
                item["content_sha256"] = content_hash.hexdigest()
                item["file_type"] = "regular"
                item["mode"] = 0o100755 if info.st_mode & stat.S_IXUSR else 0o100644
            # Deleted files have no worktree stat to inspect. Check the base
            # blob size before asking Git to materialize a potentially huge
            # patch for them.
            base_size = _run_git(["cat-file", "-s", f"{base_sha}:{path}"], cwd=str(repo), check=False)
            if base_size.returncode == 0 and base_size.stdout.strip().isdigit() and int(base_size.stdout.strip()) > _DIFF_MAX_FILE_BYTES:
                item.update(truncated=True, reason="Base file exceeds the per-file review limit.")
                truncated = True
                files.append(item)
                continue
            # --literal-pathspecs (a global git flag, set once below) already
            # makes every pathspec on this command line literal -- it must
            # never be combined with an explicit ":(literal)" magic prefix
            # on the path itself: the two cancel out (the literal prefix
            # string becomes part of the literal path git looks for, which
            # then matches nothing and git silently returns an empty diff,
            # exit 0, no error). Confirmed live: with both, `git diff`
            # against a real modified file returned "" instead of a patch.
            args = ["--literal-pathspecs", "diff", "--no-ext-diff", "--no-textconv", "--no-color", "--unified=3", "--find-renames", base_sha, "--", path]
            patch = _run_git(args, cwd=str(repo)).stdout
            if "\0" in patch or patch.startswith("Binary files ") or "\nBinary files " in patch:
                item.update(binary=True, content_omitted=True, reason="Binary file content is omitted.")
                patch = None
            else:
                item["additions"] = sum(1 for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++"))
                item["deletions"] = sum(1 for line in patch.splitlines() if line.startswith("-") and not line.startswith("---"))
        if patch is not None:
            size = len(patch.encode("utf-8"))
            if size > _DIFF_MAX_FILE_BYTES or total_bytes + size > _DIFF_MAX_TOTAL_BYTES:
                item.update(truncated=True, reason="Patch omitted because the review size limit was reached.")
                truncated = True
            else:
                item["patch"] = patch
                total_bytes += size
        files.append(item)

    for item in files:
        if item.get("content_omitted") and item.get("reason") == "Sensitive file content is omitted.":
            item["sensitive"] = True
    fingerprint_data = [(f["path"], f.get("old_path"), f["status"], f.get("file_type"), f.get("mode"), f.get("content_sha256"), hashlib.sha256((f["patch"] or "").encode("utf-8")).hexdigest(), f["reason"]) for f in files]
    review_id = hashlib.sha256(json.dumps([base_sha, head_sha, fingerprint_data, truncated], ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
    approval_ready = not truncated and not any(item.get("sensitive") or item.get("file_type") == "symlink" for item in files)
    return {"base_sha": base_sha, "head_sha": head_sha, "files": files, "file_count": len(files),
            "additions": sum(f["additions"] or 0 for f in files),
            "deletions": sum(f["deletions"] or 0 for f in files), "truncated": truncated,
            "approval_ready": approval_ready,
            "truncation_reason": "File count or patch size limit reached." if truncated else None,
            "review_id": review_id}


def create_dev_worktree(project_path: str, session_id: str) -> str:
    """Create one isolated development worktree for a session.

    Returns the new worktree's absolute path. Raises a DevWorktreeError
    subclass (see module docstring) on any validation or Git failure;
    nothing is cleaned up automatically on failure.
    """
    branch = branch_name_for_session(session_id)
    repo = _require_git_repo(project_path)
    head_sha = _head_sha(repo)

    root = dev_worktree_root()
    target = root / session_id

    # Collision checks -- directory and branch, independently, before any
    # mutation is attempted.
    if target.exists():
        raise WorktreeCollisionError(f"a directory already exists at {target}")
    if _branch_exists(repo, branch):
        raise WorktreeCollisionError(f"branch {branch!r} already exists in {repo}")
    existing = _run_git(["worktree", "list", "--porcelain"], cwd=str(repo)).stdout
    target_key = os.path.normcase(os.path.normpath(str(target)))
    for line in existing.splitlines():
        if line.startswith("worktree "):
            registered = line[len("worktree "):].strip()
            if os.path.normcase(os.path.normpath(registered)) == target_key:
                raise WorktreeCollisionError(
                    f"{target} is already registered as a worktree of {repo}"
                )

    _run_git(
        ["worktree", "add", "-b", branch, str(target), head_sha],
        cwd=str(repo),
    )

    # Confirm the new worktree is genuinely linked to the expected repo
    # and commit -- never trust the command's exit code alone.
    actual_sha = _head_sha(target)
    if actual_sha != head_sha:
        raise DevWorktreeError(
            f"worktree {target} is at {actual_sha!r}, expected {head_sha!r}"
        )
    expected_common_dir = _git_common_dir(repo)
    actual_common_dir = _git_common_dir(target)
    if actual_common_dir != expected_common_dir:
        raise DevWorktreeError(
            f"worktree {target} is not linked to {repo} "
            f"(git-common-dir {actual_common_dir} != {expected_common_dir})"
        )

    return str(target)


def verify_dev_worktree(project_path: str, worktree_path: str, session_id: str) -> Path:
    """Prove a worktree genuinely belongs to this project AND this session.

    Returns the validated, resolved Path on success. Raises (never
    returns a value that was not actually verified) if: worktree_path is
    not inside the dedicated dev-worktree root (dev_worktree_root()), the
    directory does not exist, it is not a Git worktree of project_path at
    all, or it is on the wrong branch for session_id -- e.g. another
    session's worktree, even if called with a wrong (but validly-shaped)
    session_id. Read-only: never mutates anything. Shared by
    remove_dev_worktree() (below) and by nexus/dev/executor.py, which
    must confirm this before ever pointing a write-capable Codex process
    at a cwd.
    """
    branch = branch_name_for_session(session_id)
    repo = _require_git_repo(project_path)

    if not isinstance(worktree_path, str) or not worktree_path.strip():
        raise DevWorktreeError("worktree_path must not be empty")
    candidate = Path(worktree_path)
    if not candidate.is_absolute():
        raise DevWorktreeError(f"worktree_path must be absolute: {worktree_path!r}")
    if not candidate.exists():
        raise DevWorktreeError(f"worktree_path does not exist: {worktree_path!r}")

    root = dev_worktree_root()
    try:
        validated = validate_destructive_target(root, candidate)
    except UnsafeWorktreePathError as exc:
        raise DevWorktreeError(f"refusing to trust unsafe path {candidate}: {exc}") from exc

    actual_common_dir = _git_common_dir(validated)
    expected_common_dir = _git_common_dir(repo)
    if actual_common_dir != expected_common_dir:
        raise ForeignWorktreeError(f"{validated} is not a worktree of {repo}")
    current_branch = _run_git(
        ["rev-parse", "--abbrev-ref", "HEAD"], cwd=str(validated)
    ).stdout.strip()
    if current_branch != branch:
        raise ForeignWorktreeError(
            f"{validated} is on branch {current_branch!r}, expected {branch!r} "
            f"for session {session_id!r}"
        )
    return validated


def remove_dev_worktree(project_path: str, worktree_path: str, session_id: str) -> None:
    """Safely remove a clean, idle development worktree and its branch.

    Refuses (raises) rather than deleting if: the path is not proven to
    be inside the dedicated dev-worktree root, the worktree does not
    belong to this exact project_path/session_id, or the worktree has
    any tracked change or untracked file. Idempotent: if the worktree's
    resources are already gone, this returns successfully without error.
    Never uses `git worktree remove --force` and never runs
    `git worktree prune`.
    """
    branch = branch_name_for_session(session_id)
    repo = _require_git_repo(project_path)

    if not isinstance(worktree_path, str) or not worktree_path.strip():
        raise DevWorktreeError("worktree_path must not be empty")
    candidate = Path(worktree_path)
    if not candidate.is_absolute():
        raise DevWorktreeError(f"worktree_path must be absolute: {worktree_path!r}")

    if not candidate.exists():
        # Idempotent cleanup: nothing on disk, and git has no stale
        # registration for this exact path either -- the session's
        # resources are already gone.
        existing = _run_git(["worktree", "list", "--porcelain"], cwd=str(repo)).stdout
        candidate_key = os.path.normcase(os.path.normpath(str(candidate)))
        for line in existing.splitlines():
            if line.startswith("worktree "):
                registered = line[len("worktree "):].strip()
                if os.path.normcase(os.path.normpath(registered)) == candidate_key:
                    # Stale registration with no directory behind it --
                    # let git reconcile just this one entry (never a
                    # blanket `git worktree prune`).
                    _run_git(["worktree", "remove", str(candidate)], cwd=str(repo), check=False)
                    break
        _best_effort_delete_branch(repo, branch)
        return

    # Prove the target is inside our own dedicated root AND genuinely
    # belongs to this project/session before letting anything destructive
    # touch it.
    validated = verify_dev_worktree(project_path, worktree_path, session_id)

    # Refuse on any tracked change or untracked file -- git's own
    # `worktree remove` (without --force) enforces this too, but this
    # explicit check gives a clear, typed error before ever invoking it.
    status = _run_git(
        ["status", "--porcelain", "--untracked-files=all"], cwd=str(validated)
    ).stdout
    if status.strip():
        raise WorktreeNotCleanError(
            f"{validated} has tracked changes or untracked files; refusing to remove"
        )

    _run_git(["worktree", "remove", str(validated)], cwd=str(repo))
    _best_effort_delete_branch(repo, branch)


def _best_effort_delete_branch(repo: Path, branch: str) -> None:
    """Delete the session's branch only via a SAFE (-d) delete.

    Never force-deletes (-D): a branch git considers "not fully merged"
    is left in place rather than discarded, since forced discard is an
    explicit, separate, future capability this module does not
    implement. Any failure here is non-fatal to the caller -- the
    worktree itself has already been removed, which is this function's
    main safety property.
    """
    if not _branch_exists(repo, branch):
        return
    _run_git(["branch", "-d", branch], cwd=str(repo), check=False)
