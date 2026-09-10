"""Lightweight, dependency-free source size and complexity analysis."""

from pathlib import Path
import ast


class ComplexityScanner:
    def __init__(self, repository_path: str | Path): self.path = Path(repository_path).resolve()

    def scan(self) -> dict[str, object]:
        sizes = {}
        warnings = []
        for file in sorted(self.path.rglob("*.py")):
            if any(part in {".git", "__pycache__", ".venv", "venv"} for part in file.parts): continue
            name = file.relative_to(self.path).as_posix()
            try: lines = len(file.read_text(encoding="utf-8").splitlines()); tree = ast.parse(file.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, SyntaxError): continue
            sizes[name] = lines
            decisions = sum(isinstance(n, (ast.If, ast.For, ast.While, ast.Try, ast.With, ast.BoolOp, ast.comprehension)) for n in ast.walk(tree))
            if lines >= 500: warnings.append((name, "large file", lines))
            if decisions >= 25: warnings.append((name, "high decision complexity", decisions))
        largest = tuple(sorted(sizes, key=lambda x: (-sizes[x], x))[:10])
        return {"line_counts": dict(sorted(sizes.items())), "largest_modules": largest,
                "complexity_warnings": tuple(warnings)}


def scan_complexity(repository_path: str | Path) -> dict[str, object]: return ComplexityScanner(repository_path).scan()
