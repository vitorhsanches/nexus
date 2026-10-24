"""Authoritative deterministic registry of Nexus execution capabilities."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ExecutionCapability(str, Enum):
    REPOSITORY_READ = "REPOSITORY_READ"
    FILE_READ = "FILE_READ"
    FILE_WRITE = "FILE_WRITE"
    SHELL_EXECUTION = "SHELL_EXECUTION"
    TEST_EXECUTION = "TEST_EXECUTION"
    LOCAL_GIT = "LOCAL_GIT"
    DATA_ANALYSIS = "DATA_ANALYSIS"
    WEB_RESEARCH = "WEB_RESEARCH"
    DOCUMENT_GENERATION = "DOCUMENT_GENERATION"
    EXTERNAL_API = "EXTERNAL_API"
    EXTERNAL_DEPLOYMENT = "EXTERNAL_DEPLOYMENT"


SUPPORTED_EXECUTION_CAPABILITIES = frozenset({
    ExecutionCapability.REPOSITORY_READ,
    ExecutionCapability.FILE_READ,
    ExecutionCapability.FILE_WRITE,
    ExecutionCapability.SHELL_EXECUTION,
    ExecutionCapability.TEST_EXECUTION,
    ExecutionCapability.LOCAL_GIT,
})


@dataclass(frozen=True)
class CapabilityResolution:
    required: tuple[str, ...]
    supported: tuple[str, ...]
    unsupported: tuple[str, ...]

    @property
    def all_supported(self) -> bool:
        return not self.unsupported

    def to_dict(self) -> dict:
        return {
            "required": list(self.required),
            "supported": list(self.supported),
            "unsupported": list(self.unsupported),
            "all_supported": self.all_supported,
        }


def is_execution_capability_supported(
    capability: ExecutionCapability | str,
) -> bool:
    try:
        value = (
            capability
            if isinstance(capability, ExecutionCapability)
            else ExecutionCapability(capability)
        )
    except ValueError:
        return False
    return value in SUPPORTED_EXECUTION_CAPABILITIES


def resolve_capabilities(required: tuple[str, ...]) -> CapabilityResolution:
    unique = tuple(dict.fromkeys(required))
    supported = tuple(
        item for item in unique if is_execution_capability_supported(item)
    )
    unsupported = tuple(
        item for item in unique if not is_execution_capability_supported(item)
    )
    return CapabilityResolution(unique, supported, unsupported)
