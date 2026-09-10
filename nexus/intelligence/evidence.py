"""Structured, read-only evidence derived from intelligence findings."""

from dataclasses import dataclass
from typing import Iterable

from .errors import IntelligenceValidationError
from .models import IntelligenceFinding, IntelligenceScanResult


@dataclass(frozen=True, slots=True)
class Evidence:
    source_file: str
    source_module: str
    line_reference: str | None
    evidence_type: str
    description: str

    def __post_init__(self) -> None:
        for value, name in ((self.source_file, "source_file"),
                            (self.source_module, "source_module"),
                            (self.evidence_type, "evidence_type"),
                            (self.description, "description")):
            if not isinstance(value, str) or not value.strip():
                raise IntelligenceValidationError(f"{name} must be a non-empty string")
        if self.line_reference is not None and (not isinstance(self.line_reference, str) or not self.line_reference.strip()):
            raise IntelligenceValidationError("line_reference must be a non-empty string or None")


class EvidenceEngine:
    """Turn scan references into stable, structured evidence records."""

    def generate(self, scan: IntelligenceScanResult) -> tuple[Evidence, ...]:
        if not isinstance(scan, IntelligenceScanResult):
            raise IntelligenceValidationError("scan must be an IntelligenceScanResult")
        result: list[Evidence] = []
        for finding in scan.findings:
            for reference in finding.evidence_references:
                file_name, line = self._split_reference(reference)
                module = file_name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
                kind = self._evidence_type(finding)
                result.append(Evidence(file_name, module, line, kind,
                                       f"{finding.title}: {finding.description}"))
        return tuple(sorted(result, key=lambda item: (item.source_file, item.line_reference or "", item.evidence_type)))

    create = generate

    @staticmethod
    def _split_reference(reference: str) -> tuple[str, str | None]:
        if ":" in reference:
            path, suffix = reference.rsplit(":", 1)
            if suffix.isdigit():
                return path, suffix
        return reference, None

    @staticmethod
    def _evidence_type(finding: IntelligenceFinding) -> str:
        text = (finding.title + " " + finding.description).lower()
        if "complex" in text:
            return "complexity"
        if "depend" in text or "coupling" in text:
            return "dependency"
        if "change" in text:
            return "repository-change"
        return "finding"


def generate_evidence(scan: IntelligenceScanResult) -> tuple[Evidence, ...]:
    return EvidenceEngine().generate(scan)
