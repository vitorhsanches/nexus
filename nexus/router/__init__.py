import re
import unicodedata
from dataclasses import dataclass

from nexus.registry.projects import list_projects_for_routing


class ProjectNotFoundError(Exception):
    """Raised when no registered project matches the routing query."""


class ProjectAmbiguousError(Exception):
    """Raised when more than one registered project matches the routing query."""


@dataclass(slots=True)
class RoutedProject:
    id: str
    name: str
    path: str
    aliases: list[str]
    enabled: bool


def _normalize(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.lower().strip()
    value = re.sub(r"\s+", " ", value)
    return value


def _candidates(enabled_only: bool = True) -> list[dict]:
    return [
        project
        for project in list_projects_for_routing()
        if not enabled_only or project["enabled"]
    ]


def _word_boundary_pattern(term: str) -> re.Pattern:
    return re.compile(r"(?<![\w])" + re.escape(term) + r"(?![\w])")


def resolve_project(query: str) -> RoutedProject:
    """Resolve an explicit project id, name, or alias."""
    if not query or not query.strip():
        raise ProjectNotFoundError("PROJECT_NOT_FOUND: empty routing query.")

    projects = _candidates()

    if not projects:
        raise ProjectNotFoundError(
            "PROJECT_NOT_FOUND: no projects are registered."
        )

    query_stripped = query.strip()

    # 1. explicit project id or project name (exact match).
    for project in projects:
        if query_stripped == project["id"] or query_stripped == project["name"]:
            return _to_routed(project)

    # 2. exact alias match.
    exact_alias_matches = [
        project
        for project in projects
        if query_stripped in project["aliases"]
    ]

    if len(exact_alias_matches) == 1:
        return _to_routed(exact_alias_matches[0])

    if len(exact_alias_matches) > 1:
        names = ", ".join(
            sorted(project["name"] for project in exact_alias_matches)
        )
        raise ProjectAmbiguousError(
            f"PROJECT_AMBIGUOUS: query {query!r} matches multiple projects: {names}."
        )

    # 3. normalized case-insensitive alias/name matching.
    normalized_query = _normalize(query_stripped)

    matches = []

    for project in projects:
        normalized_candidates = {
            _normalize(project["id"]),
            _normalize(project["name"]),
            *[_normalize(alias) for alias in project["aliases"]],
        }

        if normalized_query in normalized_candidates:
            matches.append(project)

    unique_matches = _dedupe(matches)

    if len(unique_matches) == 1:
        return _to_routed(unique_matches[0])

    if len(unique_matches) > 1:
        names = ", ".join(sorted(project["name"] for project in unique_matches))
        raise ProjectAmbiguousError(
            f"PROJECT_AMBIGUOUS: query {query!r} matches multiple projects: {names}."
        )

    raise ProjectNotFoundError(
        f"PROJECT_NOT_FOUND: no registered project matches {query!r}."
    )


def resolve_project_from_text(text: str) -> RoutedProject:
    """Resolve a project mentioned inside a free-form natural-language request."""
    if not text or not text.strip():
        raise ProjectNotFoundError("PROJECT_NOT_FOUND: empty request text.")

    projects = _candidates()

    if not projects:
        raise ProjectNotFoundError(
            "PROJECT_NOT_FOUND: no projects are registered."
        )

    normalized_text = _normalize(text)

    matched_projects = []

    for project in projects:
        terms = {project["id"], project["name"], *project["aliases"]}
        normalized_terms = {_normalize(term) for term in terms if term.strip()}

        for term in normalized_terms:
            if _word_boundary_pattern(term).search(normalized_text):
                matched_projects.append(project)
                break

    unique_matches = _dedupe(matched_projects)

    if len(unique_matches) == 1:
        return _to_routed(unique_matches[0])

    if len(unique_matches) > 1:
        names = ", ".join(sorted(project["name"] for project in unique_matches))
        raise ProjectAmbiguousError(
            f"PROJECT_AMBIGUOUS: request matches multiple projects: {names}."
        )

    raise ProjectNotFoundError(
        "PROJECT_NOT_FOUND: no registered project could be determined from the request."
    )


def _dedupe(projects: list[dict]) -> list[dict]:
    seen = {}

    for project in projects:
        seen[project["id"]] = project

    return list(seen.values())


def _to_routed(project: dict) -> RoutedProject:
    return RoutedProject(
        id=project["id"],
        name=project["name"],
        path=project["path"],
        aliases=list(project["aliases"]),
        enabled=bool(project["enabled"]),
    )
