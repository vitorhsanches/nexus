import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from nexus.program import verify


class VerificationEnvTests(unittest.TestCase):
    def test_nexus_interpreter_directory_leads_path(self):
        env = verify.verification_env()
        path_key = next(k for k in env if k.upper() == "PATH")
        first = env[path_key].split(os.pathsep)[0]
        self.assertEqual(first, os.path.dirname(sys.executable))

    def test_unrelated_system_python_earlier_on_path_cannot_win(self):
        other = r"C:\SomeOtherPython\Scripts"
        with patch.dict(os.environ, {"PATH": other + os.pathsep + "C:\\Windows"}):
            env = verify.verification_env()
        path_key = next(k for k in env if k.upper() == "PATH")
        entries = env[path_key].split(os.pathsep)
        self.assertEqual(entries[0], os.path.dirname(sys.executable))
        self.assertLess(entries.index(os.path.dirname(sys.executable)), entries.index(other))

    def test_temp_and_tmp_remain_inherited(self):
        with patch.dict(os.environ, {"TEMP": r"C:\T1", "TMP": r"C:\T2"}):
            env = verify.verification_env()
        self.assertEqual(env["TEMP"], r"C:\T1")
        self.assertEqual(env["TMP"], r"C:\T2")

    def test_ci_true_is_injected(self):
        self.assertEqual(verify.verification_env()["CI"], "true")

    def test_run_command_keeps_cwd_and_uses_verification_env(self):
        cwd = Path(r"C:\some\verify\worktree")
        process = MagicMock()
        process.communicate.return_value = ("ok", None)
        process.returncode = 0
        with patch.object(verify.subprocess, "Popen", return_value=process) as popen:
            result = verify.run_command("python -m unittest", cwd, timeout=None)
        kwargs = popen.call_args.kwargs
        self.assertEqual(kwargs["cwd"], str(cwd))
        self.assertTrue(kwargs["shell"])
        self.assertEqual(kwargs["env"]["CI"], "true")
        self.assertEqual(
            kwargs["env"][next(k for k in kwargs["env"] if k.upper() == "PATH")].split(os.pathsep)[0],
            os.path.dirname(sys.executable),
        )
        self.assertTrue(result["passed"])


if __name__ == "__main__":
    unittest.main()
