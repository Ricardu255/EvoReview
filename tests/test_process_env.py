"""Guard the single restricted-environment allowlist for repository subprocesses.

``RepositoryToolSuite`` (scanners, repository checks) and ``RepairVerifier``
(repair test gate) both execute code from a reviewed repository.  They used to
carry two independent allowlists, so the repair gate silently dropped Windows
cache variables that the tool suite forwarded.
"""
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from evoagent.adapters.verifier import RepairVerifier
from evoagent.agent.repository_tools import RepositoryToolSuite
from evoagent.process_env import ALLOWED_ENV_KEYS, restricted_env


class RestrictedEnvTests(unittest.TestCase):
    def test_secrets_are_not_forwarded(self):
        fake = {
            "PATH": "/usr/bin",
            "APPDATA": "C:/Users/x/AppData/Roaming",
            "EVOAGENT_AUTH_SECRET": "leak",
            "EVOAGENT_DEEPSEEK_API_KEY": "leak",
            "GITHUB_TOKEN": "leak",
            "AWS_SECRET_ACCESS_KEY": "leak",
            "DATABASE_URL": "postgresql://user:pass@host/db",
        }
        with mock.patch.dict(os.environ, fake, clear=True):
            env = restricted_env()
        self.assertEqual({"PATH", "APPDATA"}, set(env))
        self.assertEqual("/usr/bin", env["PATH"])

    def test_no_environment_variable_reaches_a_child_process_by_default(self):
        with mock.patch.dict(os.environ, {"SECRET": "leak"}, clear=True):
            self.assertEqual({}, restricted_env())

    def test_extra_values_are_explicit_only(self):
        with mock.patch.dict(
            os.environ, {"TMPDIR": "/from-env", "SECRET": "leak"}, clear=True,
        ):
            env = restricted_env({"TMPDIR": "/override"})
        self.assertEqual({"TMPDIR": "/override"}, env)


class SharedAllowlistTests(unittest.TestCase):
    def test_tool_suite_resolves_through_the_shared_helper(self):
        fake = {"APPDATA": "C:/AppData", "PATH": "p", "SECRET": "leak"}
        with mock.patch.dict(os.environ, fake, clear=True):
            self.assertEqual(restricted_env(), RepositoryToolSuite._restricted_env())

    def test_windows_cache_variables_are_allowlisted(self):
        for key in ("APPDATA", "LOCALAPPDATA", "USERPROFILE", "SYSTEMROOT"):
            self.assertIn(key, ALLOWED_ENV_KEYS)

    def test_verifier_forwards_the_shared_allowlist(self):
        captured = {}

        def fake_run(command, **kwargs):
            captured.update(kwargs)
            return subprocess.CompletedProcess(command, 0, "", "")

        with tempfile.TemporaryDirectory() as root:
            verifier = RepairVerifier("python -m compileall .", timeout_seconds=5)
            with mock.patch.dict(
                os.environ,
                {"APPDATA": "C:/AppData", "GITHUB_TOKEN": "leak"},
                clear=True,
            ), mock.patch(
                "evoagent.adapters.verifier.subprocess.run", fake_run
            ):
                result = verifier.verify_worktree(root)

        self.assertTrue(result["passed"])
        self.assertEqual("C:/AppData", captured["env"]["APPDATA"])
        self.assertNotIn("GITHUB_TOKEN", captured["env"])
        self.assertIn("TMPDIR", captured["env"])


if __name__ == "__main__":
    unittest.main()
