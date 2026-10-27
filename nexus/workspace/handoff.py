"""Explicit, bounded transfer of a reviewed mission snapshot between worktrees.

No mission, model, Git integration, or coordinator resume is started here.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path, PurePosixPath

from nexus.chat.appserver_client import BACKEND_CODEX_NATIVE
from nexus.dev.executor import session_backend
from nexus.dev.service import DevServiceError, validate_dev_project
from nexus.dev.worktree import (
    DevWorktreeError,
    read_worktree_diff,
    read_worktree_head_sha,
    verify_dev_worktree,
)
from nexus.registry import dev_integrations, dev_missions, dev_sessions
from nexus.registry import workspace as panes
from nexus.registry import workspace_handoff as records

_WINDOWS_RESERVED = re.compile(r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$", re.IGNORECASE)
_PROTECTED_DIRS = {".git", ".github", ".codex", ".aws", ".ssh", ".venv", "venv", "node_modules", "__pycache__"}
_MAX_FILE_BYTES = 128 * 1024


def _fail(code: str, message: str, status: int = 409):
    raise DevServiceError(code, message, http_status=status)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True,
        encoding="utf-8", errors="surrogateescape", check=False,
    )
    if result.returncode:
        _fail("HANDOFF_GIT_ERROR", "Unable to verify Git worktree state.")
    return result.stdout


def _safe_path(root: Path, path: str, *, must_exist: bool) -> Path:
    if not isinstance(path, str) or not path or "\\" in path or "\0" in path or ":" in path:
        _fail("HANDOFF_UNSAFE_PATH", "Unsupported path in handoff snapshot.")
    pure = PurePosixPath(path)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in path.split("/")):
        _fail("HANDOFF_UNSAFE_PATH", "Path escapes the worktree.")
    if pure.parts[0].casefold() == "config" or tuple(part.casefold() for part in pure.parts[:2]) == ("nexus", "storage"):
        _fail("HANDOFF_UNSAFE_PATH", "Protected configuration or storage path.")
    for part in pure.parts:
        if (part.casefold() in _PROTECTED_DIRS or part.endswith((" ", "."))
                or _WINDOWS_RESERVED.match(part) or any(ord(c) < 32 for c in part)
                or any(c in '<>"|?*' for c in part)):
            _fail("HANDOFF_UNSAFE_PATH", "Protected or non-portable path in handoff snapshot.")
    current = root
    for part in pure.parts:
        if current.is_dir():
            with os.scandir(current) as entries:
                if any(entry.name.casefold() == part.casefold() and entry.name != part for entry in entries):
                    _fail("HANDOFF_CASE_COLLISION", "Path collides with an existing case variant.")
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or (hasattr(current, "is_junction") and current.is_junction()):
            _fail("HANDOFF_SYMLINK", "Symlink component in handoff path.")
    try:
        current.relative_to(root)
    except ValueError:
        _fail("HANDOFF_UNSAFE_PATH", "Path escapes the worktree.")
    if must_exist:
        try:
            info = current.lstat()
        except OSError:
            _fail("HANDOFF_FILE_MISSING", "A handoff file is missing.")
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            _fail("HANDOFF_UNSAFE_FILE", "Handoff files must be regular and have no hardlinks.")
    return current


def _bytes(path: Path) -> bytes:
    with path.open("rb") as source:
        data = source.read(_MAX_FILE_BYTES + 1)
    if len(data) > _MAX_FILE_BYTES:
        _fail("HANDOFF_FILE_TOO_LARGE", "File exceeds the bounded handoff size.")
    if b"\0" in data:
        _fail("HANDOFF_BINARY", "Binary content cannot be transferred.")
    try:
        data.decode("utf-8")
    except UnicodeError:
        _fail("HANDOFF_BINARY", "Non-UTF-8 content cannot be transferred.")
    return data


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _check_case_collision(path: str, status: str, target_exists: bool,
                          tracked: set[str], seen: set[str]) -> None:
    folded = path.casefold()
    if folded in seen:
        _fail("HANDOFF_CASE_COLLISION", "Snapshot paths collide on a case-insensitive filesystem.")
    seen.add(folded)
    if (status == "added" and (target_exists or tracked)
            or status == "modified" and tracked != {path}):
        _fail("HANDOFF_TARGET_CONFLICT", "Target path conflicts with an existing file.")


def _identity(source_pane_id: str, target_pane_id: str) -> tuple:
    source, target = panes.get_pane(source_pane_id), panes.get_pane(target_pane_id)
    if source is None or target is None:
        _fail("HANDOFF_PANE_NOT_FOUND", "Source or target agent was not found.", 404)
    if (source["id"] == target["id"] or not source["plan_id"]
            or source["plan_id"] != target["plan_id"]
            or source["project_id"] != target["project_id"]):
        _fail("HANDOFF_IDENTITY_MISMATCH", "Agents must be distinct members of one project and plan.")
    brief = json.loads(target["brief_json"] or "{}")
    if source["plan_agent_key"] not in (brief.get("depends_on") or []):
        _fail("HANDOFF_NOT_DEPENDENCY", "The target does not declare this source as a dependency.")
    return source, target


def _mission_evidence(mission_id: str, dev_session_id: str) -> tuple[list, str | None]:
    """OmniRoute source evidence: the exact, latest, conclusively finished mission."""
    mission = dev_missions.get_mission(mission_id)
    if mission is None or mission["dev_session_id"] != dev_session_id:
        _fail("HANDOFF_MISSION_MISMATCH", "The mission does not belong to the source agent.")
    if mission["status"] != "READY_FOR_REVIEW" or mission["error_code"]:
        _fail("HANDOFF_MISSION_NOT_READY", "The source mission is not conclusively ready for review.")
    missions = dev_missions.list_missions(dev_session_id)
    if not missions or missions[0]["id"] != mission_id:
        _fail("HANDOFF_MISSION_STALE", "Only the source agent's latest mission can be transferred.")
    try:
        result = json.loads(mission["result_json"] or "{}")
        changed = result.get("changed_files") or []
        review_fingerprint = (result.get("review") or {}).get("review_id")
    except (TypeError, ValueError, KeyError):
        _fail("HANDOFF_MISSION_EVIDENCE", "Mission change evidence is invalid.")
    return changed, review_fingerprint


def _native_evidence(review_id: str, dev_session_id: str) -> tuple[list, str | None]:
    """Codex Native source evidence: the exact, latest, still-REVIEWED
    integration review -- never the diff.review_id content fingerprint,
    which identifies content, not a persisted decision; approval_ready
    means the snapshot could be completely reviewed, not that a human has
    consented to anything -- that consent is the handoff's own approve().
    """
    review = dev_integrations.get_review(review_id)
    if review is None or review["dev_session_id"] != dev_session_id:
        _fail("HANDOFF_REVIEW_MISMATCH", "The review does not belong to the source agent.")
    if review["status"] != "REVIEWED" or review["error_code"]:
        _fail("HANDOFF_REVIEW_NOT_READY", "The source review is not conclusively ready for review.")
    latest = dev_integrations.list_reviews(dev_session_id)
    if not latest or latest[0]["review_id"] != review_id:
        _fail("HANDOFF_REVIEW_STALE", "Only the source agent's most recent review can be transferred.")
    try:
        # dev_integrations.get_review() already returns the parsed
        # "snapshot" dict, not the raw "snapshot_json" column.
        snapshot = review["snapshot"]
        if not snapshot.get("approval_ready"):
            _fail("HANDOFF_REVIEW_EVIDENCE", "The source review's snapshot is incomplete.")
        diff_section = snapshot.get("diff") or {}
        changed = diff_section.get("files") or []
        review_fingerprint = diff_section.get("review_id")
    except (TypeError, ValueError, KeyError):
        _fail("HANDOFF_REVIEW_EVIDENCE", "Review change evidence is invalid.")
    return changed, review_fingerprint


def _snapshot(source_pane_id: str, target_pane_id: str, source_evidence_id: str) -> dict:
    try:
        return _build_snapshot(source_pane_id, target_pane_id, source_evidence_id)
    except (DevWorktreeError, OSError) as error:
        _fail("HANDOFF_WORKTREE_INVALID", f"Worktree verification failed: {error}")


def _build_snapshot(source_pane_id: str, target_pane_id: str, source_evidence_id: str) -> dict:
    source, target = _identity(source_pane_id, target_pane_id)
    project_path = validate_dev_project(source["project_id"])
    source_session = dev_sessions.get_session(source["dev_session_id"])
    target_session = dev_sessions.get_session(target["dev_session_id"])
    if not source_session or not target_session or source_session["status"] != "ACTIVE" or target_session["status"] != "ACTIVE":
        _fail("HANDOFF_SESSION_INACTIVE", "Both development sessions must be active.")
    is_native = session_backend(source_session) == BACKEND_CODEX_NATIVE
    if is_native:
        evidence_backend = "codex_native"
        changed, evidence_review_fingerprint = _native_evidence(source_evidence_id, source["dev_session_id"])
    else:
        evidence_backend = "omniroute"
        changed, evidence_review_fingerprint = _mission_evidence(source_evidence_id, source["dev_session_id"])
    if (dev_missions.get_active_mission_for_session(source_session["id"])
            or dev_sessions.get_active_turn_for_session(source_session["id"])):
        _fail("HANDOFF_SOURCE_BUSY", "The source agent is executing.")
    source_root = verify_dev_worktree(project_path, source_session["worktree_path"], source_session["id"])
    target_root = verify_dev_worktree(project_path, target_session["worktree_path"], target_session["id"])
    source_head = read_worktree_head_sha(str(source_root))
    target_head = read_worktree_head_sha(str(target_root))
    if source_session["base_sha"] != target_session["base_sha"] or source_head != source_session["base_sha"] or target_head != target_session["base_sha"]:
        _fail("HANDOFF_BASE_MISMATCH", "Worktrees require the same unchanged base commit.")
    if _git(target_root, "status", "--porcelain=v1", "-z", "--untracked-files=all"):
        _fail("HANDOFF_TARGET_DIRTY", "The target worktree already contains changes.")
    if dev_missions.get_active_mission_for_session(target_session["id"]) or dev_sessions.get_active_turn_for_session(target_session["id"]):
        _fail("HANDOFF_TARGET_BUSY", "The target agent is executing.")
    diff = read_worktree_diff(str(source_root), source_session["base_sha"])
    try:
        # changed_files (OmniRoute) is already "added"/"modified"; a native
        # review's diff.files can still say "untracked" -- normalize both
        # sides the same way "actual" below always has been.
        expected = {(row["path"], "added" if row["status"] == "untracked" else row["status"]) for row in changed}
    except (TypeError, KeyError):
        _fail("HANDOFF_MISSION_EVIDENCE", "Change evidence is invalid.")
    if not expected or len(expected) != len(changed) or diff["truncated"]:
        _fail("HANDOFF_MISSION_EVIDENCE", "Change evidence is empty, duplicated or truncated.")
    if evidence_review_fingerprint != diff["review_id"]:
        _fail("HANDOFF_SOURCE_DRIFT", "Source snapshot differs from the review captured when the source finished.")
    actual = {(row["path"], row["status"]) for row in diff["files"]}
    # Existing changed_files labels untracked as added; Git review uses untracked.
    actual = {(path, "added" if status == "untracked" else status) for path, status in actual}
    if actual != expected:
        _fail("HANDOFF_SOURCE_DRIFT", "Source files no longer match the recorded evidence.")
    tracked_target = _git(target_root, "ls-files", "-z").split("\0")
    by_case = {}
    for path in tracked_target:
        if path:
            by_case.setdefault(path.casefold(), set()).add(path)
    files = []
    seen = set()
    for item in diff["files"]:
        path = item["path"]
        status = "added" if item["status"] == "untracked" else item["status"]
        if (status not in {"added", "modified"} or item.get("binary") or item.get("content_omitted")
                or item.get("truncated") or item.get("sensitive") or item.get("old_path")
                or item.get("file_type") not in {"regular", "text"} or item.get("patch") is None
                or item.get("mode") != 0o100644
                or any(line.startswith(("old mode ", "new mode ")) for line in item["patch"].splitlines())):
            _fail("HANDOFF_UNSUPPORTED_FILE", "The snapshot contains an unsupported file operation or content.")
        source_file = _safe_path(source_root, path, must_exist=True)
        target_file = _safe_path(target_root, path, must_exist=status == "modified")
        collisions = by_case.get(path.casefold(), set())
        _check_case_collision(path, status, target_file.exists(), collisions, seen)
        raw = _bytes(source_file)
        if _hash(raw) != item.get("content_sha256"):
            _fail("HANDOFF_SOURCE_DRIFT", "Source content changed during preview.")
        files.append({"path": path, "status": status, "sha256": _hash(raw), "size": len(raw)})
    snapshot = {
        "project_id": source["project_id"], "plan_id": source["plan_id"],
        "source_agent_key": source["plan_agent_key"], "target_agent_key": target["plan_agent_key"],
        "source_pane_id": source["id"], "target_pane_id": target["id"],
        "source_backend": evidence_backend,
        "source_mission_id": source_evidence_id if evidence_backend == "omniroute" else None,
        "source_native_review_id": source_evidence_id if evidence_backend == "codex_native" else None,
        "source_session_id": source_session["id"],
        "target_session_id": target_session["id"], "source_base_sha": source_session["base_sha"],
        "source_head_sha": source_head, "target_base_sha": target_session["base_sha"],
        "target_head_sha": target_head, "files": files,
    }
    payload = [snapshot, diff["review_id"]]
    snapshot["snapshot_fingerprint"] = _hash(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8"))
    return snapshot


def _same(record, snapshot: dict) -> bool:
    return (record["snapshot_fingerprint"] == snapshot["snapshot_fingerprint"]
            and json.loads(record["files_json"]) == snapshot["files"]
            and all(record[key] == snapshot[key] for key in snapshot if key not in {"files", "snapshot_fingerprint"}))


def _evidence_id(record) -> str:
    """The one source evidence id a handoff record actually carries --
    a mission_id for 'omniroute', a native_review_id for 'codex_native'.
    The schema's own CHECK constraint already guarantees exactly one of
    the two columns is set; this never guesses which from the value's
    shape."""
    return record["source_mission_id"] or record["source_native_review_id"]


def preview(source_pane_id: str, target_pane_id: str, source_evidence_id: str) -> dict:
    snapshot = _snapshot(source_pane_id, target_pane_id, source_evidence_id)
    if records.target_has_unresolved_transfer(snapshot["target_session_id"]):
        _fail("HANDOFF_TARGET_UNCERTAIN", "A previous transfer to this target needs human review.")
    return records.create(snapshot)


def approve(handoff_id: str, fingerprint: str, source_pane_id: str, target_pane_id: str) -> dict:
    record = records.get(handoff_id)
    if record is None:
        _fail("HANDOFF_NOT_FOUND", "Handoff was not found.", 404)
    if record["source_pane_id"] != source_pane_id or record["target_pane_id"] != target_pane_id or fingerprint != record["snapshot_fingerprint"]:
        _fail("HANDOFF_APPROVAL_MISMATCH", "Approval does not match the reviewed snapshot or agents.")
    if record["status"] not in {"PREVIEWED", "APPROVED"}:
        _fail("HANDOFF_NOT_PREVIEWED", "Only a preview can be approved.")
    try:
        snapshot = _snapshot(source_pane_id, target_pane_id, _evidence_id(record))
        if not _same(record, snapshot):
            _fail("HANDOFF_SNAPSHOT_CHANGED", "Snapshot changed; create and approve a new preview.")
    except DevServiceError as error:
        records.block(handoff_id, record["status"], error.code, str(error))
        raise
    if record["status"] == "APPROVED":
        return records.view(record)
    if not records.approve(handoff_id, fingerprint):
        _fail("HANDOFF_APPROVAL_RACE", "Another operation changed this preview.")
    return records.view(records.get(handoff_id))


def transfer(handoff_id: str) -> dict:
    record = records.get(handoff_id)
    if record is None:
        _fail("HANDOFF_NOT_FOUND", "Handoff was not found.", 404)
    if record["status"] == "TRANSFERRED":
        return records.view(record)
    if record["status"] != "APPROVED":
        _fail("HANDOFF_NOT_APPROVED", "Only an approved handoff can be transferred.")
    try:
        snapshot = _snapshot(record["source_pane_id"], record["target_pane_id"], _evidence_id(record))
        if not _same(record, snapshot):
            _fail("HANDOFF_SNAPSHOT_CHANGED", "Snapshot changed; create and approve a new preview.")
    except DevServiceError as error:
        records.block(handoff_id, "APPROVED", error.code, str(error))
        raise
    token = records.claim(handoff_id)
    if token is None:
        _fail("HANDOFF_CONCURRENT", "A source or target execution or transfer prevents this claim.")
    # The committed TRANSFERRING row is the write-ahead barrier. A crash here
    # leaves the row unreplayable; no startup recovery invokes this function.
    try:
        snapshot = _snapshot(record["source_pane_id"], record["target_pane_id"], _evidence_id(record))
        if not _same(record, snapshot):
            _fail("HANDOFF_SNAPSHOT_CHANGED", "Snapshot changed immediately before transfer.")
        source = dev_sessions.get_session(record["source_session_id"])
        target = dev_sessions.get_session(record["target_session_id"])
        source_root, target_root = Path(source["worktree_path"]), Path(target["worktree_path"])
        for item in snapshot["files"]:
            if not records.owns(handoff_id, token):
                _fail("HANDOFF_FENCE_LOST", "Transfer ownership was lost before writing.")
            source_file = _safe_path(source_root, item["path"], must_exist=True)
            target_file = _safe_path(target_root, item["path"], must_exist=item["status"] == "modified")
            content = _bytes(source_file)
            if _hash(content) != item["sha256"]:
                _fail("HANDOFF_SOURCE_DRIFT", "Source content changed before writing.")
            if item["status"] == "added":
                target_file.parent.mkdir(parents=True, exist_ok=True)
                # Exclusive creation prevents overwriting an unexpected file.
                with target_file.open("xb") as output:
                    output.write(content)
                    output.flush()
                    os.fsync(output.fileno())
            else:
                # The target was proven clean against the same base immediately
                # before this barrier. Recheck each file before opening it.
                if _git(target_root, "status", "--porcelain=v1", "--", item["path"]):
                    _fail("HANDOFF_TARGET_DRIFT", "Target file changed during transfer.")
                # Replace one file atomically. A crash can leave an extra
                # temporary file, which remains for human review; it cannot
                # truncate the prior target or a linked project file.
                original_mode = stat.S_IMODE(target_file.stat().st_mode)
                with tempfile.NamedTemporaryFile(prefix=".nexus-handoff-", dir=target_file.parent,
                                                 delete=False) as output:
                    output.write(content)
                    output.flush()
                    os.fsync(output.fileno())
                    staged_path = Path(output.name)
                os.chmod(staged_path, original_mode)
                if not records.owns(handoff_id, token):
                    _fail("HANDOFF_FENCE_LOST", "Transfer ownership was lost before replacement.")
                os.replace(staged_path, target_file)
            if _hash(_bytes(target_file)) != item["sha256"]:
                _fail("HANDOFF_VERIFY_FAILED", "Transferred file hash did not match source.")
        result = {"files": snapshot["files"], "verified": True, "integration_applied": False}
        if not records.finish(handoff_id, token, "TRANSFERRED", result=result):
            _fail("HANDOFF_FENCE_LOST", "Transfer ownership was lost before recording success.")
    except Exception as error:
        records.finish(handoff_id, token, "RESULT_UNCERTAIN", error_code=getattr(error, "code", "HANDOFF_WRITE_INTERRUPTED"), error_message=str(error)[:2000])
        raise
    return records.view(records.get(handoff_id))


def status(handoff_id: str) -> dict:
    record = records.view(records.get(handoff_id))
    if record is None:
        _fail("HANDOFF_NOT_FOUND", "Handoff was not found.", 404)
    return record


def history(plan_id: str) -> list[dict]:
    if panes.get_plan(plan_id) is None:
        _fail("PLAN_NOT_FOUND", "Plan was not found.", 404)
    return records.list_for_plan(plan_id)


def verified_dependency(plan_id: str, source_step, target_step) -> dict:
    """Read-only proof that the exact completed step reached the target intact.

    This deliberately uses the persisted step's own evidence (mission_id or
    native_review_id), never the session's latest mission/review. It is
    also called immediately before a resumed launch.
    """
    source = panes.get_pane(source_step["pane_id"])
    target = panes.get_pane(target_step["pane_id"])
    source_evidence_id = source_step["native_review_id"] or source_step["mission_id"]
    if source is None or target is None or not source_evidence_id:
        _fail("RESUME_DEPENDENCY_INVALID", "Dependency identity or evidence is missing.")
    if (source["plan_id"] != plan_id or target["plan_id"] != plan_id
            or source["project_id"] != target["project_id"]
            or source["plan_agent_key"] != source_step["agent_key"]
            or target["plan_agent_key"] != target_step["agent_key"]):
        _fail("RESUME_DEPENDENCY_INVALID", "Dependency does not belong to this plan.")
    _identity(source["id"], target["id"])
    source_session_for_backend = dev_sessions.get_session(source["dev_session_id"])
    if source_session_for_backend is None:
        _fail("RESUME_DEPENDENCY_INVALID", "The source development session no longer exists.")
    is_native = session_backend(source_session_for_backend) == BACKEND_CODEX_NATIVE
    # Mirrors the original ordering exactly: confirm the source is
    # conclusively ready, THEN ask whether any transfer exists at all
    # (DEPENDENCY_WORKTREE_NOT_TRANSFERRED is the common, actionable
    # "you haven't handed off yet" case) -- and only once a transfer
    # record actually exists, re-derive and validate the review
    # fingerprint/changed-files to cross-check it. Parsing that evidence
    # before the transfer-existence check would surface a confusing
    # RESUME_EVIDENCE_INVALID for the ordinary "not transferred yet" case.
    if is_native:
        evidence_backend = "codex_native"
        if source_step["mission_id"] or not source_step["native_review_id"]:
            _fail("RESUME_DEPENDENCY_INVALID", "Native dependency evidence is inconsistent.")
        review = dev_integrations.get_review(source_step["native_review_id"])
        if (review is None or review["dev_session_id"] != source["dev_session_id"]
                or review["status"] != "REVIEWED" or review["error_code"]
                or source_step["status"] != "READY_FOR_REVIEW" or source_step["error_code"]):
            _fail("RESUME_SOURCE_INVALID", "The persisted source review is not conclusively ready.")
    else:
        evidence_backend = "omniroute"
        if source_step["native_review_id"] or not source_step["mission_id"]:
            _fail("RESUME_DEPENDENCY_INVALID", "Mission dependency evidence is inconsistent.")
        mission = dev_missions.get_mission(source_step["mission_id"])
        if (mission is None or mission["dev_session_id"] != source["dev_session_id"]
                or mission["status"] != "READY_FOR_REVIEW" or mission["error_code"]
                or source_step["status"] != "READY_FOR_REVIEW" or source_step["error_code"]):
            _fail("RESUME_SOURCE_INVALID", "The persisted source mission is not conclusively ready.")
    try:
        record = records.latest_transfer(evidence_backend, source_evidence_id, target["id"])
    except (TypeError, ValueError, KeyError):
        _fail("RESUME_EVIDENCE_INVALID", "Persisted handoff result cannot be decoded.")
    if record is None or record["status"] != "TRANSFERRED":
        _fail("DEPENDENCY_WORKTREE_NOT_TRANSFERRED", "The exact source evidence has no completed handoff.")
    if is_native:
        try:
            snapshot = review["snapshot"]
            diff_section = snapshot.get("diff") or {}
            review_id = diff_section.get("review_id")
            changed = diff_section.get("files") or []
        except (TypeError, ValueError, AttributeError, KeyError):
            _fail("RESUME_EVIDENCE_INVALID", "Source review evidence is invalid.")
        if not isinstance(review_id, str) or not review_id:
            _fail("RESUME_EVIDENCE_INVALID", "Source review has no durable review fingerprint.")
        # dev_integrations reviews have their own status machine
        # (REVIEWED -> APPROVED/APPLYING/APPLIED/REJECTED/...); already-
        # resolved reviews fail the REVIEWED check above, so there is no
        # separate embedded "integration_applied" flag to re-check here.
    else:
        try:
            mission_result = json.loads(mission["result_json"] or "{}")
            review_id = (mission_result.get("review") or {}).get("review_id")
            changed = mission_result.get("changed_files") or []
        except (TypeError, ValueError, AttributeError):
            _fail("RESUME_EVIDENCE_INVALID", "Source mission review evidence is invalid.")
        if not isinstance(review_id, str) or not review_id:
            _fail("RESUME_EVIDENCE_INVALID", "Source mission has no durable review fingerprint.")
        if mission_result.get("integration_applied") is True:
            _fail("RESUME_SOURCE_INVALID", "Source mission was already integrated outside this sequence.")
    source_session = dev_sessions.get_session(source["dev_session_id"])
    target_session = dev_sessions.get_session(target["dev_session_id"])
    if (not source_session or not target_session or source_session["status"] != "ACTIVE"
            or target_session["status"] != "ACTIVE"
            or records.target_has_unresolved_transfer(target_session["id"])):
        _fail("RESUME_TARGET_UNCERTAIN", "The target transfer is unresolved.")
    result = record["result"]
    fingerprint = record["snapshot_fingerprint"]
    if (record["project_id"] != source["project_id"] or record["plan_id"] != plan_id
            or record["source_agent_key"] != source_step["agent_key"]
            or record["target_agent_key"] != target_step["agent_key"]
            or record["source_pane_id"] != source["id"] or record["target_pane_id"] != target["id"]
            or record["source_backend"] != evidence_backend
            or (record["source_mission_id"] or record["source_native_review_id"]) != source_evidence_id
            or record["source_session_id"] != source_session["id"]
            or record["target_session_id"] != target_session["id"]
            or record["source_base_sha"] != source_session["base_sha"]
            or record["target_base_sha"] != target_session["base_sha"]
            or record["source_base_sha"] != record["target_base_sha"]
            or not record["approved_at"] or not record["finished_at"]
            or not isinstance(fingerprint, str) or len(fingerprint) != 64
            or record["error_code"] or not isinstance(result, dict)
            or result.get("verified") is not True
            or result.get("integration_applied") is not False
            or result.get("files") != record["files"]):
        _fail("RESUME_EVIDENCE_INVALID", "Persisted handoff evidence is incomplete or mismatched.")
    snapshot_keys = (
        "project_id", "plan_id", "source_agent_key", "target_agent_key",
        "source_pane_id", "target_pane_id", "source_backend", "source_mission_id",
        "source_native_review_id", "source_session_id",
        "target_session_id", "source_base_sha", "source_head_sha",
        "target_base_sha", "target_head_sha",
    )
    approved_snapshot = {key: record[key] for key in snapshot_keys}
    approved_snapshot["files"] = record["files"]
    fingerprint = _hash(json.dumps([approved_snapshot, review_id], sort_keys=True, ensure_ascii=False).encode("utf-8"))
    if fingerprint != record["snapshot_fingerprint"]:
        _fail("RESUME_EVIDENCE_INVALID", "Handoff fingerprint no longer matches the source review.")
    if not isinstance(record["files"], list) or not isinstance(changed, list):
        _fail("RESUME_EVIDENCE_INVALID", "Changed-file evidence has an invalid shape.")
    try:
        expected_changes = {(item["path"], item["status"]) for item in record["files"]}
        recorded_changes = {(item["path"], "added" if item["status"] == "untracked" else item["status"])
                            for item in changed}
    except (TypeError, KeyError):
        _fail("RESUME_EVIDENCE_INVALID", "Source changed-file list is invalid.")
    if not expected_changes or len(changed) != len(record["files"]) or recorded_changes != expected_changes:
        _fail("RESUME_EVIDENCE_INVALID", "Source change evidence differs from the handoff.")
    project_path = validate_dev_project(source["project_id"])
    try:
        source_root = verify_dev_worktree(project_path, source_session["worktree_path"], source_session["id"])
        target_root = verify_dev_worktree(project_path, target_session["worktree_path"], target_session["id"])
        if read_worktree_head_sha(str(source_root)) != record["source_head_sha"]:
            _fail("RESUME_SOURCE_INVALID", "Source Git HEAD changed after approval.")
        if read_worktree_head_sha(str(target_root)) != record["target_head_sha"]:
            _fail("RESUME_TARGET_DRIFT", "The target Git HEAD changed after transfer.")
        source_diff = read_worktree_diff(str(source_root), source_session["base_sha"])
        diff = read_worktree_diff(str(target_root), target_session["base_sha"])
        if source_diff["truncated"] or source_diff["review_id"] != review_id:
            _fail("RESUME_SOURCE_INVALID", "Source content no longer matches the approved mission review.")
        source_files = {(item["path"], "added" if item["status"] == "untracked" else item["status"],
                         item.get("content_sha256")) for item in source_diff["files"]}
        expected = {(item["path"], item["status"], item["sha256"]) for item in record["files"]}
        if source_files != expected or len(source_diff["files"]) != len(record["files"]):
            _fail("RESUME_SOURCE_INVALID", "Source file hashes differ from the approved handoff.")
        actual = {(item["path"], "added" if item["status"] == "untracked" else item["status"],
                   item.get("content_sha256")) for item in diff["files"]}
        if diff["truncated"] or len(actual) != len(record["files"]) or actual != expected:
            _fail("RESUME_TARGET_DRIFT", "Target worktree differs from the approved transferred snapshot.")
        for item in record["files"]:
            if _hash(_bytes(_safe_path(target_root, item["path"], must_exist=True))) != item["sha256"]:
                _fail("RESUME_TARGET_DRIFT", "A transferred file was modified or replaced.")
    except (DevWorktreeError, OSError) as error:
        _fail("RESUME_TARGET_DRIFT", f"Target worktree verification failed: {error}")
    return record
