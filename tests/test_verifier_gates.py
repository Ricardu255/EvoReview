"""Tests for RepairVerifier compilation, worktree and archive gates."""
import io
import os
import subprocess
import tempfile
import tokenize
import unittest
import zipfile
from unittest.mock import patch

from evoagent.adapters.verifier import RepairVerifier


def archive(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        for name, content in entries:
            bundle.writestr(name, content)
    return buffer.getvalue()


class VerifyContentsTests(unittest.TestCase):
    def test_valid_python_produces_compile_and_tokenize_checks(self):
        result = RepairVerifier().verify_contents({"a.py": "value = 1\n"})
        self.assertTrue(result["passed"])
        self.assertEqual(
            ["compile:a.py", "cst-tokenize:a.py"],
            [item["name"] for item in result["checks"]],
        )
        self.assertGreaterEqual(result["duration_seconds"], 0)

    def test_non_python_files_are_ignored(self):
        result = RepairVerifier().verify_contents({"notes.txt": "not python"})
        self.assertTrue(result["passed"])
        self.assertEqual([], result["checks"])

    def test_syntax_error_is_reported_with_location(self):
        result = RepairVerifier().verify_contents({"a.py": "def broken(:\n"})
        self.assertFalse(result["passed"])
        self.assertEqual("compile:a.py", result["checks"][0]["name"])
        self.assertIn("a.py:1", result["checks"][0]["detail"])

    def test_tokenizer_failure_is_reported(self):
        with patch(
            "evoagent.adapters.verifier.tokenize.generate_tokens",
            side_effect=tokenize.TokenError("unterminated string", "(1,0)"),
        ):
            result = RepairVerifier().verify_contents({"a.py": "value = 1\n"})
        self.assertFalse(result["passed"])
        self.assertEqual("cst-tokenize:a.py", result["checks"][-1]["name"])
        self.assertTrue(result["checks"][0]["passed"])
        self.assertFalse(result["checks"][-1]["passed"])


class WorktreeTests(unittest.TestCase):
    def test_without_a_test_command_the_gate_is_a_noop(self):
        result = RepairVerifier().verify_worktree("anywhere")
        self.assertEqual({
            "passed": True, "checks": [],
            "note": "No repository test command configured.",
        }, result)

    def test_missing_worktree_is_rejected(self):
        verifier = RepairVerifier("python -c pass")
        with self.assertRaisesRegex(ValueError, "does not exist"):
            verifier.verify_worktree(os.path.join(tempfile.gettempdir(), "missing-dir-xyz"))

    def test_passing_and_failing_commands_are_measured(self):
        with tempfile.TemporaryDirectory() as root:
            passed = RepairVerifier("python -c pass").verify_worktree(root)
            self.assertTrue(passed["passed"])
            self.assertEqual("repository-tests", passed["checks"][0]["name"])

            failed = RepairVerifier("python -c exit(1)").verify_worktree(root)
            self.assertFalse(failed["passed"])
            self.assertFalse(failed["checks"][0]["passed"])

    def test_timeout_is_reported_as_a_failure(self):
        verifier = RepairVerifier("python -c pass", timeout_seconds=3)
        with tempfile.TemporaryDirectory() as root, patch(
            "evoagent.adapters.verifier.subprocess.run",
            side_effect=subprocess.TimeoutExpired("cmd", 3),
        ):
            result = verifier.verify_worktree(root)
        self.assertFalse(result["passed"])
        self.assertIn("exceeded 3 seconds", result["checks"][0]["detail"])


class ArchiveTests(unittest.TestCase):
    def test_extracts_a_single_top_level_directory_and_writes_repairs(self):
        bundle = archive([
            ("repo/app.py", "print('original')\n"),
            ("repo/README.md", "readme\n"),
        ])
        verifier = RepairVerifier("python app.py")
        with tempfile.TemporaryDirectory() as output:
            result = verifier.verify_archive(
                bundle, {"app.py": "print('repaired')\n"},
            )
        # ``python app.py`` succeeds only after the repair file is written.
        self.assertTrue(result["passed"], result["checks"])
        self.assertEqual(
            ["compile:app.py", "cst-tokenize:app.py", "repository-tests"],
            [item["name"] for item in result["checks"]],
        )

    def test_archive_without_a_single_root_directory_uses_the_root(self):
        bundle = archive([("app.py", "value = 1\n")])
        result = RepairVerifier().verify_archive(bundle, {"app.py": "value = 2\n"})
        self.assertTrue(result["passed"])

    def test_compilation_failure_short_circuits_the_test_command(self):
        bundle = archive([("repo/app.py", "value = 1\n")])
        verifier = RepairVerifier("python -c pass")
        result = verifier.verify_archive(bundle, {"app.py": "def broken(:\n"})
        self.assertFalse(result["passed"])
        self.assertEqual(
            ["compile:app.py"], [item["name"] for item in result["checks"]],
        )

    def test_failing_tests_make_the_archive_gate_fail(self):
        bundle = archive([("repo/app.py", "value = 1\n")])
        result = RepairVerifier("python -c exit(1)").verify_archive(bundle, {})
        self.assertFalse(result["passed"])
        self.assertEqual(
            ["repository-tests"], [item["name"] for item in result["checks"]],
        )

    def test_unsafe_archive_members_are_rejected(self):
        for name in ("../evil.py", "/etc/passwd"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    RepairVerifier().verify_archive(archive([(name, "x")]), {})

    def test_repair_paths_cannot_escape_the_worktree(self):
        bundle = archive([("repo/app.py", "value = 1\n")])
        with self.assertRaisesRegex(ValueError, "repair path escapes"):
            RepairVerifier().verify_archive(bundle, {"../escape.py": "x = 1\n"})


class CompareTests(unittest.TestCase):
    @staticmethod
    def result(passed, tests=True):
        checks = [{"name": "repository-tests", "passed": passed}] if tests else []
        return {"passed": passed, "checks": checks}

    def test_requires_passing_before_and_after_evidence(self):
        self.assertTrue(RepairVerifier.compare(
            self.result(True), self.result(True),
        )["passed"])
        self.assertFalse(RepairVerifier.compare(
            self.result(True), self.result(True, tests=False),
        )["passed"])
        self.assertFalse(RepairVerifier.compare(
            self.result(False), self.result(True),
        )["passed"])
        self.assertFalse(RepairVerifier.compare(
            self.result(True), self.result(False),
        )["passed"])

    def test_reports_regression_evidence(self):
        regression = RepairVerifier.compare(
            self.result(True), self.result(False),
        )
        self.assertTrue(regression["behavioral_regression_detected"])
        self.assertFalse(regression["patched_passed"])
        self.assertTrue(regression["baseline_passed"])
        self.assertTrue(regression["test_evidence_present"])

        no_evidence = RepairVerifier.compare(
            self.result(True), self.result(True, tests=False),
        )
        self.assertFalse(no_evidence["test_evidence_present"])


if __name__ == "__main__":
    unittest.main()
