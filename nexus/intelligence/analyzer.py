"""Deterministic, read-only orchestration for repository intelligence."""

from hashlib import sha256
from pathlib import Path
from typing import Any

from .models import FindingSeverity, IntelligenceFinding, IntelligenceScanResult, ScanStatus
from .scanners import ComplexityScanner, DependencyScanner, GitScanner, RepositoryScanner


class IntelligenceAnalyzer:
    """Run the intelligence scanners and turn their observations into findings."""

    DEFAULT_TIMESTAMP = "1970-01-01T00:00:00+00:00"

    def __init__(self, repository_path: str | Path):
        self.path = Path(repository_path).resolve()

    def analyze(self, captured_at: str | None = None) -> IntelligenceScanResult:
        if not self.path.is_dir():
            raise ValueError(f"invalid repository path: {self.path}")
        stamp = captured_at or self.DEFAULT_TIMESTAMP

        # RepositoryScanner supplies the canonical snapshot and is deliberately
        # retained as a separate read-only scanner invocation.
        snapshot, _ = RepositoryScanner(self.path).scan(stamp)
        git = GitScanner(self.path).scan() if (self.path / ".git").exists() else {
            "revision": snapshot.revision, "HEAD": snapshot.revision, "dirty": False,
            "modified_files": (), "untracked_files": (), "tracked_files": snapshot.file_references,
        }
        dependencies = DependencyScanner(self.path).scan()
        complexity = ComplexityScanner(self.path).scan()
        findings: list[IntelligenceFinding] = []

        def add(key: str, title: str, description: str, severity: FindingSeverity,
                evidence: tuple[str, ...], confidence: float = .9) -> None:
            evidence = tuple(sorted(set(evidence)))
            if not evidence:
                return
            digest = sha256((key + snapshot.revision + "\0" + "\0".join(evidence)).encode()).hexdigest()[:12]
            findings.append(IntelligenceFinding("F-" + digest, title, description, severity,
                                                confidence, evidence, stamp))

        large = tuple(name for name, count in complexity.get("line_counts", {}).items() if count >= 500)
        add("large-modules", "Large modules detected", f"{len(large)} module(s) exceed 500 lines.",
            FindingSeverity.MEDIUM, large)

        coupling = tuple(dependencies.get("coupling_hotspots", ()))
        add("dependency-coupling", "High dependency coupling detected",
            "Modules import at least eight distinct top-level dependencies.", FindingSeverity.MEDIUM, coupling)

        changes = tuple(sorted(tuple(git.get("modified_files", ())) + tuple(git.get("untracked_files", ()))))
        add("repository-changes", "Repository changes detected",
            f"{len(changes)} working-tree file(s) differ from the recorded revision.", FindingSeverity.LOW, changes)

        warnings = tuple(complexity.get("complexity_warnings", ()))
        add("complexity-hotspots", "Complexity hotspots detected",
            "One or more modules exceed the complexity scanner thresholds.", FindingSeverity.HIGH,
            tuple(item[0] for item in warnings))

        python_files = tuple(name for name in complexity.get("line_counts", {}) if name.endswith(".py"))
        top_level = tuple(name for name in python_files if "/" not in name)
        if len(top_level) >= 3:
            add("architecture-risk", "Flat module architecture risk",
                "Several Python modules live at repository root, which can increase boundary coupling.",
                FindingSeverity.LOW, top_level, .75)

        findings.sort(key=lambda item: item.finding_id)
        scan_id = "S-" + snapshot.repository_id + "-" + snapshot.revision[:8]
        return IntelligenceScanResult(scan_id, snapshot, ScanStatus.COMPLETED, tuple(findings), stamp, stamp)

    scan = analyze


def analyze_repository(repository_path: str | Path, captured_at: str | None = None) -> IntelligenceScanResult:
    return IntelligenceAnalyzer(repository_path).analyze(captured_at)
