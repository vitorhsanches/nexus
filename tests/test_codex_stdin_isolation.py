import ast
import unittest
from pathlib import Path
from unittest.mock import patch

import nexus.dispatchers.review as review_module

ROOT = Path(__file__).resolve().parents[1]
PROMPT_ARGUMENT_LAUNCHERS = (
    ROOT / "nexus" / "dispatchers" / "manager.py",
    ROOT / "nexus" / "program" / "architect.py",
    ROOT / "nexus" / "dispatchers" / "review.py",
)


def _popen_calls(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "Popen"
        ):
            yield node


class CodexPromptLauncherStdinTests(unittest.TestCase):
    def test_every_prompt_argument_launcher_detaches_stdin(self):
        for path in PROMPT_ARGUMENT_LAUNCHERS:
            calls = list(_popen_calls(path))
            self.assertTrue(calls, f"no Popen found in {path.name}")
            for call in calls:
                keywords = {kw.arg: kw.value for kw in call.keywords}
                self.assertIn("stdin", keywords, f"{path.name} Popen without stdin=")
                self.assertEqual(
                    ast.unparse(keywords["stdin"]),
                    "subprocess.DEVNULL",
                    f"{path.name} Popen must use stdin=subprocess.DEVNULL",
                )

    def test_default_review_launcher_passes_devnull_stdin(self):
        captured = {}

        class _FakePopen:
            def __init__(self, *args, **kwargs):
                captured.update(kwargs)
                self.stdout = iter(())

        with patch.object(review_module.subprocess, "Popen", _FakePopen):
            review_module._default_process_launcher(
                ["codex", "exec", "-C", r"C:\fake\reviewed-worktree"]
            )

        self.assertIs(captured["stdin"], review_module.subprocess.DEVNULL)


if __name__ == "__main__":
    unittest.main()
