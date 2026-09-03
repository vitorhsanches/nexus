"""Data model for Nexus capability definitions."""

from dataclasses import dataclass


@dataclass(slots=True)
class Capability:
    """A named capability an agent may declare it can satisfy."""

    name: str
    description: str
