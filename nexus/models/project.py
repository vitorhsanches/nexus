from dataclasses import dataclass, field


@dataclass(slots=True)
class Project:
    id: str
    name: str
    path: str
    aliases: list[str] = field(default_factory=list)
    enabled: bool = True
