"""Read-only Git metadata collection."""

from pathlib import Path
import subprocess


class GitScanner:
    def __init__(self, repository_path: str | Path):
        self.path = Path(repository_path).resolve()

    def _git(self, *args: str) -> str:
        return subprocess.check_output(("git", *args), cwd=self.path, text=True, stderr=subprocess.DEVNULL).strip()

    def scan(self) -> dict[str, object]:
        branch = self._git("branch", "--show-current") or "DETACHED"
        revision = self._git("rev-parse", "HEAD")
        tracked = tuple(sorted(filter(None, self._git("ls-files", "-z").split("\x00"))))
        status = self._git("status", "--porcelain=v1", "--untracked-files=all")
        modified, untracked = [], []
        for line in status.splitlines():
            if len(line) < 4:
                continue
            name = line[3:].split(" -> ", 1)[-1]
            if line[:2] == "??":
                untracked.append(name)
            else:
                modified.append(name)
        modified_files = tuple(sorted(set(modified)))
        untracked_files = tuple(sorted(set(untracked)))
        return {
            "HEAD": revision,
            "branch": branch,
            "dirty": bool(status),
            "tracked_files": tracked,
            "modified_files": modified_files,
            "untracked_files": untracked_files,
            # Compatibility aliases for callers of the initial scanner API.
            "revision": revision,
        }


def scan_git(repository_path: str | Path) -> dict[str, object]:
    return GitScanner(repository_path).scan()
