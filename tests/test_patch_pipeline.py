"""Tests for unified-patch parsing, patch application and repair fixers."""
import unittest

from evoagent.adapters.fixer import SafeFixer
from evoagent.adapters.patching import (
    SuggestionOnlyFixer, VerifiedPatchFixer, apply_file_patch, parse_unified_patch,
)


SIMPLE_PATCH = (
    "--- a/app.py\n"
    "+++ b/app.py\n"
    "@@ -1,2 +1,2 @@\n"
    "-value = eval(raw)\n"
    "+value = parse(raw)\n"
    " return value\n"
)


class ParseUnifiedPatchTests(unittest.TestCase):
    def test_parses_multiple_files_and_hunks(self):
        patch = (
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "@@ -1,2 +1,2 @@\n"
            "-old\n"
            "+new\n"
            " same\n"
            "@@ -10,1 +10,2 @@\n"
            " context\n"
            "+added\n"
            "--- a/lib.py\n"
            "+++ b/lib.py\n"
            "@@ -1 +1 @@\n"
            "-before\n"
            "+after\n"
        )
        patches = parse_unified_patch(patch, ["app.py", "lib.py"])
        self.assertEqual(["app.py", "lib.py"], [item.path for item in patches])
        self.assertEqual(2, len(patches[0].hunks))

    def test_accepts_crlf_and_timestamps_in_headers(self):
        patch = SIMPLE_PATCH.replace("\n", "\r\n").replace(
            "--- a/app.py", "--- a/app.py\t2024-01-01 00:00:00",
        )
        patches = parse_unified_patch(patch, ["app.py"])
        self.assertEqual("app.py", patches[0].path)

    def test_rejects_content_before_a_file_header(self):
        with self.assertRaisesRegex(ValueError, "outside a file header"):
            parse_unified_patch("stray\n" + SIMPLE_PATCH, ["app.py"])

    def test_rejects_missing_new_file_header(self):
        with self.assertRaisesRegex(ValueError, "missing new-file header"):
            parse_unified_patch("--- a/app.py\n", ["app.py"])

    def test_rejects_paths_that_are_not_allowed(self):
        with self.assertRaisesRegex(ValueError, "not allowed"):
            parse_unified_patch(SIMPLE_PATCH, ["other.py"])

    def test_rejects_mismatched_old_and_new_paths(self):
        patch = SIMPLE_PATCH.replace("--- a/app.py", "--- a/other.py")
        with self.assertRaisesRegex(ValueError, "not allowed"):
            parse_unified_patch(patch, ["app.py"])

    def test_rejects_absolute_and_parent_traversal_paths(self):
        with self.assertRaisesRegex(ValueError, "unsafe patch path"):
            parse_unified_patch(
                "--- a//abs.py\n+++ b//abs.py\n@@ -1 +1 @@\n-a\n+b\n", ["/abs.py"],
            )
        with self.assertRaisesRegex(ValueError, "unsafe patch path"):
            parse_unified_patch(
                "--- a/../app.py\n+++ b/../app.py\n@@ -1 +1 @@\n-a\n+b\n",
                ["../app.py"],
            )

    def test_rejects_invalid_hunk_headers_and_bodies(self):
        with self.assertRaisesRegex(ValueError, "invalid hunk header"):
            parse_unified_patch("--- a/app.py\n+++ b/app.py\n@@ nope @@\n", ["app.py"])
        with self.assertRaisesRegex(ValueError, "invalid unified patch line"):
            parse_unified_patch(
                "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n?bad\n", ["app.py"],
            )

    def test_rejects_hunks_with_wrong_line_counts(self):
        with self.assertRaisesRegex(ValueError, "line counts do not match"):
            parse_unified_patch(
                "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n-a\n+b\n", ["app.py"],
            )

    def test_rejects_files_without_hunks_and_duplicate_files(self):
        with self.assertRaisesRegex(ValueError, "no hunks"):
            parse_unified_patch("--- a/app.py\n+++ b/app.py\n", ["app.py"])
        with self.assertRaisesRegex(ValueError, "only once"):
            parse_unified_patch(
                SIMPLE_PATCH + SIMPLE_PATCH, ["app.py"],
            )

    def test_rejects_an_empty_patch_and_skips_no_newline_marks(self):
        with self.assertRaisesRegex(ValueError, "no valid file patches"):
            parse_unified_patch("\n\n", ["app.py"])
        patch = SIMPLE_PATCH + "\\ No newline at end of file\n"
        patches = parse_unified_patch(patch, ["app.py"])
        self.assertEqual(1, len(patches))

    def test_blank_lines_inside_a_hunk_are_rejected(self):
        patch = SIMPLE_PATCH + "\n" + SIMPLE_PATCH.replace("app.py", "lib.py")
        with self.assertRaisesRegex(ValueError, "invalid unified patch line"):
            parse_unified_patch(patch, ["app.py", "lib.py"])


class ApplyFilePatchTests(unittest.TestCase):
    def test_applies_multiple_hunks_in_one_file(self):
        source = "one\ntwo\nthree\nfour\nfive\nsix\n"
        patch = parse_unified_patch(
            "--- a/app.py\n+++ b/app.py\n"
            "@@ -1,2 +1,2 @@\n-one\n+ONE\n two\n"
            "@@ -5,2 +5,2 @@\n-five\n+FIVE\n six\n",
            ["app.py"],
        )[0]
        self.assertEqual("ONE\ntwo\nthree\nfour\nFIVE\nsix\n",
                         apply_file_patch(source, patch))

    def test_insertions_and_deletions(self):
        insertion = parse_unified_patch(
            "--- a/app.py\n+++ b/app.py\n@@ -2,0 +3,1 @@\n+inserted\n",
            ["app.py"],
        )[0]
        self.assertEqual("one\ninserted\ntwo\nthree\n",
                         apply_file_patch("one\ntwo\nthree\n", insertion))

        deletion = parse_unified_patch(
            "--- a/app.py\n+++ b/app.py\n@@ -2,1 +2,0 @@\n-two\n",
            ["app.py"],
        )[0]
        self.assertEqual("one\nthree\n", apply_file_patch("one\ntwo\nthree\n", deletion))

    def test_preserves_trailing_newline_state(self):
        patch = parse_unified_patch(SIMPLE_PATCH, ["app.py"])[0]
        with_newline = apply_file_patch("value = eval(raw)\nreturn value\n", patch)
        self.assertTrue(with_newline.endswith("\n"))
        without = apply_file_patch("value = eval(raw)\nreturn value", patch)
        self.assertFalse(without.endswith("\n"))
        self.assertEqual("value = parse(raw)\nreturn value", without)

    def test_context_mismatch_is_rejected(self):
        patch = parse_unified_patch(SIMPLE_PATCH, ["app.py"])[0]
        with self.assertRaisesRegex(ValueError, "context does not match"):
            apply_file_patch("different\nreturn value\n", patch)

    def test_overlapping_or_out_of_range_hunks_are_rejected(self):
        patch = parse_unified_patch(
            "--- a/app.py\n+++ b/app.py\n@@ -3,1 +3,1 @@\n-c\n+C\n"
            "@@ -1,1 +1,1 @@\n-a\n+A\n",
            ["app.py"],
        )[0]
        with self.assertRaisesRegex(ValueError, "overlapping or out-of-range"):
            apply_file_patch("a\nb\nc\n", patch)


class FakeVerifier:
    def __init__(self, structural=True, comparison=True, test_command=""):
        self.structural = structural
        self.comparison = comparison
        self.test_command = test_command
        self.archive_calls = 0

    def verify_contents(self, files):
        return {"passed": self.structural, "checks": []}

    def verify_archive(self, archive, files):
        self.archive_calls += 1
        return {"passed": self.structural, "checks": [
            {"name": "repository-tests", "passed": self.structural},
        ]}

    def compare(self, before, after):
        return {"passed": self.comparison}


class FakeGitHub:
    def __init__(self, content="value = eval(raw)\nreturn value\n"):
        self.content = content
        self.commits = []
        self.drafts = []

    def get_pull_request(self, repository, number):
        return {
            "head": {"ref": "feature", "sha": "head-sha", "repo": {"full_name": "fork/repo"}},
            "base": {"ref": "main"},
        }

    def get_file(self, repository, path, ref):
        self.repository = repository
        return {"decoded_content": self.content}

    def download_archive(self, repository, ref):
        return b"zip-bytes"

    def create_atomic_commit(self, repository, branch, parent_sha, files, message):
        self.commits.append((repository, branch, parent_sha, files, message))
        return {"sha": "commit-sha"}

    def create_draft_pull_request(self, repository, title, head, base, body):
        self.drafts.append((repository, title, head, base, body))
        return {"number": 9, "html_url": "https://example.test/pr/9"}


class FakeChat:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def complete_json(self, role, system, user, ledger, max_tokens):
        self.calls.append((role, system, user, max_tokens))
        return self.payload


def report_for(path="app.py", gate_passed=True):
    return {"findings": [{
        "path": path,
        "line": 1,
        "gate": {"passed": gate_passed},
        "fix": "parse it",
    }]}


class VerifiedPatchFixerTests(unittest.TestCase):
    def test_no_eligible_path_is_suggestion_only(self):
        chat = FakeChat({"patch": SIMPLE_PATCH})
        result = VerifiedPatchFixer(chat, FakeVerifier()).create_fix_commits(
            FakeGitHub(), "org/repo", 5, report_for(gate_passed=False),
        )
        self.assertEqual("suggestion-only", result["status"])
        self.assertEqual([], chat.calls)

    def test_empty_patch_is_suggestion_only(self):
        chat = FakeChat({"patch": "", "reason": "not enough evidence"})
        result = VerifiedPatchFixer(chat, FakeVerifier()).create_fix_commits(
            FakeGitHub(), "org/repo", 5, report_for(),
        )
        self.assertEqual("suggestion-only", result["status"])
        self.assertEqual("not enough evidence", result["reason"])

    def test_invalid_patch_raises(self):
        chat = FakeChat({"patch": "not a unified diff"})
        with self.assertRaises(ValueError):
            VerifiedPatchFixer(chat, FakeVerifier()).create_fix_commits(
                FakeGitHub(), "org/repo", 5, report_for(),
            )

    def test_patch_without_effective_change_raises(self):
        chat = FakeChat({
            "patch": "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n value = eval(raw)\n",
        })
        with self.assertRaisesRegex(ValueError, "makes no change"):
            VerifiedPatchFixer(chat, FakeVerifier()).create_fix_commits(
                FakeGitHub(), "org/repo", 5, report_for(),
            )

    def test_structural_failure_blocks_publication(self):
        chat = FakeChat({"patch": SIMPLE_PATCH})
        result = VerifiedPatchFixer(
            chat, FakeVerifier(structural=False, test_command="pytest"),
        ).create_fix_commits(FakeGitHub(), "org/repo", 5, report_for())
        self.assertEqual("blocked", result["status"])
        self.assertFalse(result["verification"]["structural"]["passed"])

    def test_missing_test_command_returns_a_suggestion_only(self):
        chat = FakeChat({"patch": SIMPLE_PATCH})
        result = VerifiedPatchFixer(chat, FakeVerifier()).create_fix_commits(
            FakeGitHub(), "org/repo", 5, report_for(),
        )
        self.assertEqual("suggestion-only", result["status"])
        self.assertIn("No repository test command", result["note"])
        self.assertEqual([], result["commits"])

    def test_before_after_regression_blocks_publication(self):
        verifier = FakeVerifier(comparison=False, test_command="pytest")
        result = VerifiedPatchFixer(FakeChat({"patch": SIMPLE_PATCH}), verifier) \
            .create_fix_commits(FakeGitHub(), "org/repo", 5, report_for())
        self.assertEqual("blocked", result["status"])
        self.assertFalse(result["verification"]["comparison"]["passed"])
        self.assertEqual(2, verifier.archive_calls)

    def test_verified_patch_is_published_as_a_draft(self):
        github = FakeGitHub()
        verifier = FakeVerifier(test_command="pytest")
        result = VerifiedPatchFixer(
            FakeChat({
                "patch": SIMPLE_PATCH,
                "behavioral_claims": ["eval removed"],
                "related_tests": ["tests/test_app.py"],
            }),
            verifier,
        ).create_fix_commits(github, "org/repo", 5, report_for())

        self.assertEqual("verified-draft", result["status"])
        self.assertTrue(result["branch"].startswith("evoagent/fix-pr-5-"))
        self.assertEqual("commit-sha", result["commits"][0]["sha"])
        self.assertEqual(["app.py"], result["commits"][0]["paths"])
        self.assertEqual(9, result["draft_pull_request"]["number"])
        self.assertEqual(["eval removed"], result["behavioral_claims"])
        self.assertEqual("head-sha", result["source_sha"])
        self.assertEqual("fork/repo", github.repository)
        self.assertEqual(2, verifier.archive_calls)
        self.assertEqual("main", github.drafts[0][3])


class SafeFixerTests(unittest.TestCase):
    def test_propose_line_handles_each_supported_rule(self):
        fixer = SafeFixer()
        self.assertEqual("", fixer.propose_line("print(x)", {"rule_id": "REL-DEBUG-PRINT"}))
        self.assertEqual(
            "run(cmd, shell=False)",
            fixer.propose_line("run(cmd, shell=True)", {"rule_id": "SEC-SUBPROCESS-SHELL"}),
        )
        self.assertEqual(
            '    token = os.environ["TOKEN"]',
            fixer.propose_line('    token = "abc"', {"rule_id": "SEC-HARDCODED-SECRET"}),
        )
        self.assertEqual(
            "keep = value",
            fixer.propose_line("keep = value", {"rule_id": "SEC-HARDCODED-SECRET"}),
        )
        self.assertEqual("same()", fixer.propose_line("same()", {"rule_id": "OTHER"}))

    def test_ast_repair_skips_tuple_assignment_and_unsupported_rules(self):
        content = 'a, b = "secret"\nresult = eval(raw)\n'
        findings = [
            {"path": "app.py", "line": 1, "rule_id": "SEC-HARDCODED-SECRET"},
            {"path": "app.py", "line": 2, "rule_id": "SEC-EVAL"},
        ]
        result = SafeFixer().apply(content, findings, "app.py")
        self.assertEqual([], result["rules"])
        self.assertEqual(content, result["content"])

    def test_falls_back_to_line_edits_for_non_python_files(self):
        result = SafeFixer().apply(
            'api_key = "secret-value"\n', [
                {"path": "config.yml", "line": 1, "rule_id": "SEC-HARDCODED-SECRET"},
            ], "config.yml",
        )
        self.assertEqual({"SEC-HARDCODED-SECRET"}, set(result["rules"]))
        self.assertTrue(result["content"].startswith("import os\n"))
        self.assertIn('api_key = os.environ["API_KEY"]', result["content"])

    def test_line_fallback_runs_when_python_cannot_be_parsed(self):
        content = "def broken(:\n    run(cmd, shell=True)\n"
        result = SafeFixer().apply(
            content, [{"path": "app.py", "line": 2, "rule_id": "SEC-SUBPROCESS-SHELL"}],
            "app.py",
        )
        self.assertEqual({"SEC-SUBPROCESS-SHELL"}, set(result["rules"]))
        self.assertIn("shell=False", result["content"])

    def test_ignores_findings_for_other_paths_and_out_of_range_lines(self):
        content = "value = 1\n"
        result = SafeFixer().apply(content, [
            {"path": "other.py", "line": 1, "rule_id": "REL-DEBUG-PRINT"},
            {"path": "app.py", "line": 99, "rule_id": "REL-DEBUG-PRINT"},
        ], "app.py")
        self.assertEqual(content, result["content"])
        self.assertEqual([], result["rules"])

    def test_ast_repair_changes_shell_true_keyword(self):
        content = "import subprocess\n\nresult = subprocess.run(cmd, shell=True)\n"
        result = SafeFixer().apply(
            content,
            [{"path": "app.py", "line": 3, "rule_id": "SEC-SUBPROCESS-SHELL"}],
            "app.py",
        )
        self.assertEqual({"SEC-SUBPROCESS-SHELL"}, set(result["rules"]))
        self.assertIn("shell=False", result["content"])

    def test_ast_repair_leaves_untargeted_print_statements(self):
        content = 'print("keep")\nvalue = 1\n'
        result = SafeFixer().apply(
            content,
            [{"path": "app.py", "line": 1, "rule_id": "SEC-EVAL"}],
            "app.py",
        )
        self.assertEqual(content, result["content"])
        self.assertEqual([], result["rules"])

    def test_ast_repair_does_not_duplicate_an_existing_os_import(self):
        content = 'import os\n\napi_key = "value"\n'
        result = SafeFixer().apply(
            content, [{"path": "app.py", "line": 3, "rule_id": "SEC-HARDCODED-SECRET"}],
            "app.py",
        )
        self.assertEqual(1, result["content"].count("import os"))
        self.assertTrue(
            "os.environ['API_KEY']" in result["content"]
            or 'os.environ["API_KEY"]' in result["content"]
        )


class SafeFixerPublicationTests(unittest.TestCase):
    def setUp(self):
        self.github = FakeGitHub(content='token = "secret"\n')
        self.fixer = SafeFixer(verifier=FakeVerifier())

    def _report(self, rule="SEC-HARDCODED-SECRET"):
        return {"findings": [{"path": "app.py", "line": 1, "rule_id": rule}]}

    def test_no_eligible_findings_produces_no_branch(self):
        result = self.fixer.create_fix_commits(
            self.github, "org/repo", 5, {"findings": [{
                "path": "app.py", "line": 1, "rule_id": "SEC-EVAL",
            }]},
        )
        self.assertIsNone(result["branch"])
        self.assertEqual([], result["commits"])
        self.assertIn("No finding", result["note"])

    def test_failed_verification_blocks_the_fix(self):
        self.fixer.verifier = FakeVerifier(structural=False)
        result = self.fixer.create_fix_commits(
            self.github, "org/repo", 5, self._report(),
        )
        self.assertIsNone(result["branch"])
        self.assertFalse(result["verification"]["passed"])
        self.assertIn("blocked", result["note"])

    def test_successful_fix_creates_one_draft_commit(self):
        result = self.fixer.create_fix_commits(
            self.github, "org/repo", 5, self._report(),
        )
        self.assertTrue(result["branch"].startswith("evoagent/fix-pr-5-"))
        self.assertEqual(["SEC-HARDCODED-SECRET"], result["commits"][0]["rules"])
        self.assertEqual(["app.py"], result["commits"][0]["paths"])
        self.assertEqual(9, result["draft_pull_request"]["number"])
        self.assertEqual("fork/repo", self.github.repository)

    def test_configured_test_command_runs_archive_verification(self):
        verifier = FakeVerifier(test_command="pytest")
        fixer = SafeFixer(verifier=verifier)
        fixer.create_fix_commits(self.github, "org/repo", 5, self._report())
        self.assertEqual(1, verifier.archive_calls)


class SuggestionOnlyFixerTests(unittest.TestCase):
    def test_returns_finding_suggestions(self):
        result = SuggestionOnlyFixer().create_fix_commits(
            None, "org/repo", 1, {"findings": [{"fix": "do better"}]},
        )
        self.assertEqual("suggestion-only", result["status"])
        self.assertEqual(["do better"], result["suggestions"])
        self.assertIsNone(result["branch"])


if __name__ == "__main__":
    unittest.main()
