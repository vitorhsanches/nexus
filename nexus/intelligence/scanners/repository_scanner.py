"""Facade for a complete, read-only repository intelligence scan."""

from datetime import datetime, timezone
from pathlib import Path
import hashlib
import subprocess

from ..models import (FindingSeverity, IntelligenceFinding, IntelligenceScanResult,
                      RepositorySnapshot, ScanStatus)
from .complexity_scanner import ComplexityScanner
from .dependency_scanner import DependencyScanner
from .git_scanner import GitScanner


class RepositoryScanner:
    def __init__(self, repository_path: str | Path): self.path = Path(repository_path).resolve()

    def scan(self, captured_at: str | None = None) -> tuple[RepositorySnapshot, IntelligenceScanResult]:
        if not self.path.is_dir(): raise ValueError(f"invalid repository path: {self.path}")
        stamp = captured_at or datetime.now(timezone.utc).isoformat()
        try: git = GitScanner(self.path).scan()
        except (OSError, subprocess.SubprocessError):
            git = {"branch": "UNKNOWN", "revision": "NO_GIT", "HEAD": "NO_GIT",
                   "dirty": False, "tracked_files": (), "modified_files": (), "untracked_files": ()}
        tracked = tuple(git["tracked_files"])
        if not tracked:
            tracked = tuple(sorted(p.relative_to(self.path).as_posix() for p in self.path.rglob("*")
                                   if p.is_file() and not any(x in {".git", "__pycache__", ".venv", "venv"} for x in p.parts)))
        revision = str(git["HEAD"])
        snapshot = RepositorySnapshot(hashlib.sha256(str(self.path).encode()).hexdigest()[:16], revision, stamp, tracked)
        deps, complexity = DependencyScanner(self.path).scan(), ComplexityScanner(self.path).scan()
        findings = []
        def add(key, title, description, severity, evidence):
            findings.append(IntelligenceFinding("F-" + hashlib.sha256((key + snapshot.revision).encode()).hexdigest()[:12], title, description, severity, .9, tuple(evidence), stamp))
        if git["modified_files"] or git["untracked_files"]:
            add("git", "Working tree changes detected", f"{len(git['modified_files'])} modified and {len(git['untracked_files'])} untracked files.", FindingSeverity.LOW, tuple(git["modified_files"]) + tuple(git["untracked_files"]))
        if deps["coupling_hotspots"]: add("coupling", "Import coupling hotspot", "Modules import many distinct top-level packages.", FindingSeverity.MEDIUM, deps["coupling_hotspots"])
        if complexity["complexity_warnings"]: add("complexity", "Complexity warnings detected", "One or more Python modules exceed scanner complexity thresholds.", FindingSeverity.MEDIUM, tuple(x[0] for x in complexity["complexity_warnings"]))
        result = IntelligenceScanResult("S-" + snapshot.repository_id + "-" + snapshot.revision[:8], snapshot, ScanStatus.COMPLETED, tuple(findings), stamp, stamp)
        return snapshot, result


def scan_repository(repository_path: str | Path, captured_at: str | None = None):
    return RepositoryScanner(repository_path).scan(captured_at)
