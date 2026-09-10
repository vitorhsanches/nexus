"""Static Python import and dependency relationship analysis."""

import ast
from pathlib import Path


class DependencyScanner:
    def __init__(self, repository_path: str | Path):
        self.path = Path(repository_path).resolve()

    def scan(self) -> dict[str, object]:
        graph: dict[str, set[str]] = {}
        for file in sorted(self.path.rglob("*.py")):
            if any(part in {".git", "__pycache__", ".venv", "venv"} for part in file.parts):
                continue
            name = file.relative_to(self.path).as_posix()
            try: tree = ast.parse(file.read_text(encoding="utf-8"), filename=name)
            except (OSError, UnicodeError, SyntaxError): continue
            imports = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import): imports.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module: imports.add(node.module.split(".")[0])
            graph[name] = imports
        counts: dict[str, int] = {}
        for imports in graph.values():
            for module in imports: counts[module] = counts.get(module, 0) + 1
        central = tuple(sorted(counts, key=lambda x: (-counts[x], x))[:10])
        hotspots = tuple(sorted((name for name, imports in graph.items() if len(imports) >= 8)))
        return {"imports": {k: tuple(sorted(v)) for k, v in sorted(graph.items())},
                "central_modules": central, "coupling_hotspots": hotspots}


def scan_dependencies(repository_path: str | Path) -> dict[str, object]:
    return DependencyScanner(repository_path).scan()
