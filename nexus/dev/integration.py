"""Human-approved, snapshot-bound Development Mode Git integration (F3.2).

Method: build a candidate commit from the persisted base tree using a private
temporary index and Git plumbing, without touching the destination index or
refs. Revalidate the snapshot and clean destination, then fast-forward the
destination to that exact candidate commit. Conflicts are blocked before the
branch update. A failed/uncertain Git update is recorded for inspection and is
never automatically reset or rolled back.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import threading
from pathlib import Path, PurePosixPath
from uuid import uuid4

from nexus.dev import integration_gate
from nexus.dev.service import DevServiceError, validate_dev_project
from nexus.dev.worktree import DevWorktreeError, read_worktree_diff, verify_dev_worktree
from nexus.registry import dev_integrations, dev_missions, dev_sessions

_apply_lock = threading.Lock()
_ID_RE = re.compile(r"^[0-9a-f]{64}$")


def _error(code: str, message: str, status: int = 409):
    raise DevServiceError(code, message, http_status=status)


def _git(args: list[str], *, cwd: str, env: dict | None = None, input_bytes: bytes | None = None,
         check: bool = True) -> subprocess.CompletedProcess:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    result = subprocess.run(["git", *args], cwd=cwd, env=merged, input=input_bytes,
                            capture_output=True, text=input_bytes is None,
                            encoding="utf-8" if input_bytes is None else None, errors="replace" if input_bytes is None else None)
    if check and result.returncode:
        detail = result.stderr.strip()[-800:] if isinstance(result.stderr, str) else (result.stderr or b"").decode("utf-8", "replace")[-800:]
        raise DevWorktreeError(f"git {args[0]} failed (exit {result.returncode}): {detail}")
    return result


def _safe_relative(path: str) -> bool:
    parsed = PurePosixPath(path)
    return bool(path) and not parsed.is_absolute() and all(part not in {"", ".", ".."} for part in parsed.parts) and "\\" not in path


def _authorized_session(session_id: str) -> tuple[dict, str, Path]:
    session = dev_sessions.get_session(session_id)
    if session is None:
        _error("SESSION_NOT_FOUND", "Development session not found.", 404)
    project_path = validate_dev_project(session["project_id"])
    if session["status"] != "ACTIVE":
        _error("SESSION_NOT_ACTIVE", "Only an active Development session can be integrated.")
    if not session["worktree_path"] or not session["base_sha"]:
        _error("WORKTREE_MISSING", "The session has no verified worktree/base commit.")
    try:
        worktree = verify_dev_worktree(project_path, session["worktree_path"], session_id)
    except DevWorktreeError as exc:
        raise DevServiceError("WORKTREE_INVALID", f"Development worktree validation failed: {exc}", http_status=409) from exc
    return session, project_path, worktree


def _target_state(project_path: str) -> dict:
    head = _git(["rev-parse", "HEAD"], cwd=project_path).stdout.strip()
    branch_result = _git(["symbolic-ref", "--quiet", "--short", "HEAD"], cwd=project_path, check=False)
    branch = branch_result.stdout.strip() if branch_result.returncode == 0 else None
    status = _git(["status", "--porcelain=v1", "--untracked-files=all", "-z"], cwd=project_path).stdout
    return {"head": head, "branch": branch, "dirty": bool(status),
            "dirty_paths": [part[3:] for part in status.split("\0") if len(part) >= 4]}


def _snapshot(session: dict, project_path: str, worktree: Path) -> dict:
    diff = read_worktree_diff(str(worktree), session["base_sha"])
    if diff["truncated"]:
        _error("REVIEW_INCOMPLETE", "The review is truncated; reduce the change set before requesting integration.")
    target = _target_state(project_path)
    if not target["branch"]:
        _error("TARGET_DETACHED", "The original project has no destination branch checked out.")
    payload = {"session_id": session["id"], "project_id": session["project_id"],
               "base_sha": session["base_sha"], "target_head": target["head"],
               "target_branch": target["branch"], "diff": diff}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    snapshot_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    review_id = hashlib.sha256((snapshot_hash + uuid4().hex).encode("ascii")).hexdigest()
    return {"review_id": review_id, "snapshot_hash": snapshot_hash, **payload,
            "approval_ready": bool(diff.get("approval_ready"))}


def create_review(session_id: str) -> dict:
    session, project_path, worktree = _authorized_session(session_id)
    if dev_sessions.get_active_turn_for_session(session_id) is not None:
        _error("TURN_ACTIVE", "Wait for the active Development turn to finish before reviewing integration.")
    if dev_missions.get_active_mission_for_session(session_id) is not None:
        _error("MISSION_ACTIVE", "Wait for the autonomous mission to reach a terminal review state.")
    snapshot = _snapshot(session, project_path, worktree)
    return dev_integrations.save_review(
        review_id=snapshot["review_id"], dev_session_id=session_id,
        project_id=session["project_id"], base_sha=session["base_sha"],
        target_head=snapshot["target_head"], target_branch=snapshot["target_branch"],
        snapshot=snapshot,
    )


def get_review(session_id: str, review_id: str) -> dict:
    if not _ID_RE.fullmatch(review_id or ""):
        _error("REVIEW_ID_INVALID", "Invalid review identifier.", 400)
    _authorized_session(session_id)
    record = dev_integrations.get_review(review_id)
    if not record or record["dev_session_id"] != session_id:
        _error("REVIEW_NOT_FOUND", "Review snapshot not found for this session.", 404)
    record = _reconcile_interrupted_apply(record)
    return record


def list_history(session_id: str) -> list[dict]:
    _authorized_session(session_id)
    return [_reconcile_interrupted_apply(row) for row in dev_integrations.list_reviews(session_id)]


def _reconcile_interrupted_apply(record: dict) -> dict:
    if record["status"] != "APPLYING":
        return record
    try:
        project_path = validate_dev_project(record["project_id"])
        current = _target_state(project_path)
        candidate = record.get("candidate_sha")
        if candidate and current["head"] == candidate and not current["dirty"]:
            dev_integrations.update_review(record["review_id"], status="APPLIED", result_sha=candidate)
            dev_integrations.release_project_lock(record["project_id"], record["review_id"])
        elif current["head"] == record["target_head"] and not current["dirty"]:
            dev_integrations.update_review(record["review_id"], status="FAILED", error_code="APPLY_INTERRUPTED",
                                           error_message="Apply stopped before the destination changed; create a fresh review before retrying.")
            dev_integrations.release_project_lock(record["project_id"], record["review_id"])
        else:
            dev_integrations.update_review(record["review_id"], status="APPLY_UNCERTAIN", error_code="APPLY_OUTCOME_UNCERTAIN",
                                           error_message="Destination state requires operator inspection; automatic retry is blocked.")
        updated = dev_integrations.get_review(record["review_id"])
        return updated or record
    except Exception:
        return record


def reject_review(session_id: str, review_id: str) -> dict:
    record = get_review(session_id, review_id)
    if record["status"] != "REVIEWED":
        _error("REVIEW_NOT_REJECTABLE", f"Review is {record['status']} and cannot be rejected.")
    dev_integrations.update_review(review_id, status="REJECTED")
    return {"review_id": review_id, "status": "REJECTED"}


def _validate_review(review: dict, *, require_clean_target: bool = True) -> tuple[dict, str, Path, dict, list[str]]:
    session, project_path, worktree = _authorized_session(review["dev_session_id"])
    active = dev_sessions.get_active_turn_for_session(session["id"])
    target = _target_state(project_path)
    issues = []
    if active is not None:
        issues.append({"code": "TURN_ACTIVE", "message": "A Development turn is still active."})
    if dev_missions.get_active_mission_for_session(session["id"]) is not None:
        issues.append({"code": "MISSION_ACTIVE", "message": "An autonomous mission is still active."})
    current = _snapshot_for_compare(session, project_path, worktree)
    if current["snapshot_hash"] != review["snapshot"]["snapshot_hash"]:
        issues.append({"code": "SNAPSHOT_STALE", "message": "Worktree content changed; create a new review."})
    if target["head"] != review["target_head"] or target["branch"] != review["target_branch"]:
        issues.append({"code": "TARGET_MOVED", "message": "Destination HEAD or branch changed; create a new review."})
    if target["head"] != session["base_sha"]:
        issues.append({"code": "BASE_MISMATCH", "message": "Destination must still be at the session base commit."})
    if require_clean_target and target["dirty"]:
        issues.append({"code": "TARGET_DIRTY", "message": "Destination has staged, unstaged, or untracked changes; commit or move them before integration."})
    if not review["snapshot"].get("approval_ready"):
        issues.append({"code": "REVIEW_INCOMPLETE", "message": "This snapshot contains content that cannot be completely reviewed."})
    if not review["snapshot"]["diff"]["files"]:
        issues.append({"code": "NO_CHANGES", "message": "There are no changes to integrate."})
    paths = []
    for file in review["snapshot"]["diff"]["files"]:
        if file.get("file_type") == "symlink":
            issues.append({"code": "SYMLINK_CHANGE", "message": "Symlink changes are identified in the snapshot but are not eligible for Apply in this release."})
        for candidate in (file.get("old_path"), file["path"]):
            if candidate and candidate not in paths:
                if not _safe_relative(candidate):
                    issues.append({"code": "PATH_UNSAFE", "message": "A changed path is outside the authorized worktree."})
                else:
                    paths.append(candidate)
    if paths:
        attrs = _git(["check-attr", "-z", "filter", "working-tree-encoding", "ident", "--", *paths], cwd=project_path).stdout
        atoms = attrs.split("\0")
        for index in range(0, len(atoms) - 2, 3):
            if atoms[index + 2] not in {"unspecified", "unset"}:
                issues.append({"code": "CUSTOM_CHECKOUT_FILTER", "message": "A changed path uses a Git checkout filter/encoding; integration is blocked."})
                break
        # An ignored file can be invisible to ordinary status but still block
        # or be overwritten by a checkout. Block only when a candidate path
        # intersects an existing ignored/untracked filesystem path.
        for relative in paths:
            candidate = Path(project_path) / Path(relative)
            if os.path.lexists(candidate) and not _git(["ls-files", "--error-unmatch", "--", relative], cwd=project_path, check=False).returncode == 0:
                issues.append({"code": "TARGET_PATH_COLLISION", "message": f"Destination has an untracked or ignored path that collides with {relative}."})
                break
    return session, project_path, worktree, target, issues


def _snapshot_for_compare(session: dict, project_path: str, worktree: Path) -> dict:
    diff = read_worktree_diff(str(worktree), session["base_sha"])
    target = _target_state(project_path)
    payload = {"session_id": session["id"], "project_id": session["project_id"],
               "base_sha": session["base_sha"], "target_head": target["head"],
               "target_branch": target["branch"], "diff": diff}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {"snapshot_hash": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            "approval_ready": bool(diff.get("approval_ready"))}


def integration_preflight(session_id: str, review_id: str) -> dict:
    record = get_review(session_id, review_id)
    _, _, _, target, issues = _validate_review(record)
    lock = dev_integrations.get_project_lock(record["project_id"])
    if lock and lock["review_id"] != review_id:
        issues.append({"code": "INTEGRATION_BUSY", "message": "Another integration is active or needs operator reconciliation."})
    if not integration_gate.integration_enabled():
        issues.append({"code": "INTEGRATION_DISABLED", "message": "NEXUS_DEV_INTEGRATION_ENABLED is off."})
    return {"review_id": review_id, "ready": not issues, "gate_enabled": integration_gate.integration_enabled(),
            "target_branch": target["branch"], "target_head": target["head"], "issues": issues}


def _candidate_commit(session: dict, project_path: str, worktree: Path, review: dict) -> str:
    snapshot = review["snapshot"]["diff"]
    changes = snapshot["files"]
    with tempfile.TemporaryDirectory(prefix="nexus-dev-integration-") as temp:
        index_path = os.path.join(temp, "candidate.index")
        env = {"GIT_INDEX_FILE": index_path}
        _git(["read-tree", session["base_sha"]], cwd=project_path, env=env)
        records = bytearray()
        zero = "0" * (64 if len(session["base_sha"]) == 64 else 40)
        for entry in changes:
            old_path = entry.get("old_path")
            if old_path:
                records.extend(f"0 {zero}\t".encode("ascii") + os.fsencode(old_path) + b"\0")
            relative = entry["path"]
            source = worktree / Path(relative)
            try:
                info = source.lstat()
            except OSError:
                info = None
            if info is None:
                if entry["status"] != "deleted":
                    _error("SNAPSHOT_STALE", f"Candidate file disappeared: {relative}")
                records.extend(f"0 {zero}\t".encode("ascii") + os.fsencode(relative) + b"\0")
                continue
            if stat.S_ISLNK(info.st_mode):
                content = os.fsencode(os.readlink(source)); mode = "120000"
            elif stat.S_ISREG(info.st_mode):
                content = source.read_bytes(); mode = "100755" if info.st_mode & stat.S_IXUSR else "100644"
            else:
                _error("FILE_TYPE_UNSUPPORTED", f"Cannot integrate non-regular file: {relative}")
            digest = hashlib.sha256(content).hexdigest()
            if digest != entry.get("content_sha256"):
                _error("SNAPSHOT_STALE", f"Candidate content changed while preparing integration: {relative}")
            blob = _git(["hash-object", "-w", "--stdin", "--no-filters"], cwd=project_path, input_bytes=content).stdout.decode("ascii").strip()
            records.extend(f"{mode} {blob}\t".encode("ascii") + os.fsencode(relative) + b"\0")
        if records:
            _git(["update-index", "-z", "--index-info"], cwd=project_path, env=env, input_bytes=bytes(records))
        tree = _git(["write-tree"], cwd=project_path, env=env).stdout.strip()
        candidate = _git(["-c", "user.name=Nexus Integration", "-c", "user.email=nexus-integration@localhost",
                          "commit-tree", tree, "-p", session["base_sha"], "-m",
                          f"Nexus approved Development session {session['id']}"], cwd=project_path).stdout.strip()
        actual = _git(["diff-tree", "--no-commit-id", "--name-only", "-r", "--no-renames", "-z",
                       session["base_sha"], candidate], cwd=project_path).stdout.split("\0")
        expected = set()
        for entry in changes:
            expected.add(entry["path"])
            if entry.get("old_path"):
                expected.add(entry["old_path"])
        if set(item for item in actual if item) != expected:
            _error("CANDIDATE_CONTENT_MISMATCH", "Candidate commit contains paths outside the approved snapshot.")
        return candidate


def apply_review(session_id: str, review_id: str, *, confirmed: bool) -> dict:
    if confirmed is not True:
        _error("CONFIRMATION_REQUIRED", "Explicit confirmation is required to apply this review.", 400)
    if not integration_gate.integration_enabled():
        _error("INTEGRATION_DISABLED", "NEXUS_DEV_INTEGRATION_ENABLED is off.", 403)
    if not _ID_RE.fullmatch(review_id or ""):
        _error("REVIEW_ID_INVALID", "Invalid review identifier.", 400)
    with _apply_lock:
        record = get_review(session_id, review_id)
        if record["status"] != "REVIEWED":
            _error("APPLY_ALREADY_ATTEMPTED", f"Review status is {record['status']}; create a fresh review before another attempt.")
        _, project_path, worktree, _, issues = _validate_review(record)
        if issues:
            _error(issues[0]["code"], "; ".join(item["message"] for item in issues))
        if not dev_integrations.acquire_project_lock(record["project_id"], review_id):
            _error("INTEGRATION_BUSY", "Another integration for this project is active or needs operator reconciliation.")
        candidate = None
        try:
            # Recheck authorization, session, snapshot, and clean target after
            # acquiring the cross-process lock and immediately before work.
            _, project_path, worktree, _, issues = _validate_review(record)
            if issues:
                dev_integrations.update_review(review_id, status="INVALIDATED", error_code=issues[0]["code"], error_message=" ".join(i["message"] for i in issues))
                _error(issues[0]["code"], "; ".join(item["message"] for item in issues))
            dev_integrations.update_review(review_id, status="APPROVED")
            dev_integrations.update_review(review_id, status="APPLYING")
            candidate = _candidate_commit(dev_sessions.get_session(session_id), project_path, worktree, record)
            dev_integrations.update_review(review_id, status="APPLYING", candidate_sha=candidate)
            # Required last pre-integration revalidation; candidate creation
            # changes only object storage and a private temporary index.
            _, project_path, worktree, _, issues = _validate_review(record)
            if issues:
                dev_integrations.update_review(review_id, status="INVALIDATED", candidate_sha=candidate,
                                               error_code=issues[0]["code"], error_message=" ".join(i["message"] for i in issues))
                _error(issues[0]["code"], "; ".join(item["message"] for item in issues))
            with tempfile.TemporaryDirectory(prefix="nexus-dev-hooks-") as hooks:
                merge = _git(["-c", f"core.hooksPath={hooks}", "merge", "--ff-only", "--no-edit", candidate], cwd=project_path, check=False)
            target = _target_state(project_path)
            if target["head"] == candidate and not target["dirty"]:
                dev_integrations.update_review(review_id, status="APPLIED", candidate_sha=candidate, result_sha=candidate)
                return {"review_id": review_id, "status": "APPLIED", "commit_sha": candidate, "branch": target["branch"]}
            if target["head"] == record["target_head"] and not target["dirty"]:
                dev_integrations.update_review(review_id, status="FAILED", candidate_sha=candidate,
                                               error_code="GIT_APPLY_FAILED", error_message="Fast-forward failed; original repository remains clean.")
                _error("GIT_APPLY_FAILED", "Fast-forward integration failed; the original project remains unchanged.")
            dev_integrations.update_review(review_id, status="APPLY_UNCERTAIN", candidate_sha=candidate,
                                           error_code="APPLY_OUTCOME_UNCERTAIN", error_message="Git reported an incomplete destination update; inspect repository status and branch before any retry.")
            _error("APPLY_OUTCOME_UNCERTAIN", "Git may have partially updated the destination. Do not retry; inspect the project branch and working tree.")
        except DevServiceError:
            raise
        except Exception as exc:
            # A context-manager cleanup can fail after its body has already
            # updated the destination (for example, Windows denying removal
            # of the temporary hooks directory after `git merge` returned).
            # Resolve the Git state before persisting an outcome so cleanup
            # errors cannot report FAILED after a successful integration.
            try:
                target = _target_state(project_path)
            except Exception:
                target = None
            detail = type(exc).__name__
            winerror = getattr(exc, "winerror", None)
            if winerror is not None:
                detail += f" (WinError {winerror})"
            if target is not None and candidate and target["head"] == candidate and not target["dirty"]:
                dev_integrations.update_review(review_id, status="APPLIED", candidate_sha=candidate,
                                               result_sha=candidate, error_code=None, error_message=None)
                return {"review_id": review_id, "status": "APPLIED", "commit_sha": candidate,
                        "branch": target["branch"]}
            if (target is not None and target["head"] == record["target_head"]
                    and target["branch"] == record["target_branch"] and not target["dirty"]):
                dev_integrations.update_review(review_id, status="FAILED", candidate_sha=candidate,
                                               error_code="INTEGRATION_ERROR",
                                               error_message=f"{detail}; destination verified unchanged. Create a fresh review before retrying.")
                raise DevServiceError("INTEGRATION_ERROR", "Integration failed before changing the destination; create a fresh review before retrying.", http_status=500) from exc
            dev_integrations.update_review(review_id, status="APPLY_UNCERTAIN", candidate_sha=candidate,
                                           error_code="APPLY_OUTCOME_UNCERTAIN",
                                           error_message=f"{detail}; destination state could not be confirmed. Inspect Git state; automatic retry is blocked.")
            raise DevServiceError("APPLY_OUTCOME_UNCERTAIN", "Integration outcome is uncertain; inspect the project branch and working tree. Do not retry.", http_status=500) from exc
        finally:
            latest = dev_integrations.get_review(review_id)
            if latest is None or latest["status"] not in {"APPLYING", "APPLY_UNCERTAIN"}:
                dev_integrations.release_project_lock(record["project_id"], review_id)
