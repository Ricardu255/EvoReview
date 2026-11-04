"""Deep behavioural tests for RepositoryToolSuite.

The original tool tests only cover secret-path filtering.  These exercise the
rest of the suite: listing/searching, bounded reads, symbol analysis, scanner
command construction, isolated repository checks and the registry/ledger
wrapping.
"""
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from evoagent.agent.repository_tools import RepositoryToolSuite, _sensitive_path
from evoagent.agent.runtime import ToolProtocolError
from evoagent.agent.telemetry import ExecutionLedger
from evoagent.domain.diff_parser import parse_unified_diff


DIFF = (
    "--- a/app.py\n"
    "+++ b/app.py\n"
    "@@ -1,2 +1,3 @@\n"
    " import os\n"
    "+value = eval(raw)\n"
    " return value\n"
)


def result(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class RepositoryToolSuiteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = self.temp.name
        self._write("app.py", (
            "import os\n"
            "\n"
            "\n"
            "def helper(value):\n"
            "    return os.path.basename(value)\n"
            "\n"
            "\n"
            "class Service:\n"
            "    def run(self):\n"
            "        return helper('x')\n"
        ))
        self._write(os.path.join("pkg", "other.py"),
                    "def helper():\n    return 1\n\n\ndef caller():\n    return helper()\n")
        self._write(os.path.join("pkg", "broken.py"), "def broken(:\n")
        self._write(os.path.join("tests", "test_app.py"), "def test_helper():\n    pass\n")
        self._write(os.path.join("tests", "test_helper.py"), "def test_helper():\n    pass\n")
        self._write("package.json", '{"name": "demo"}\n')
        self._write(os.path.join(".github", "workflows", "ci.yml"), "name: ci\n")
        self._write("notes.txt", "hello world\nHELLO AGAIN\n")
        self.suite = RepositoryToolSuite(self.root, DIFF, parse_unified_diff(DIFF))

    def _write(self, name, content):
        path = os.path.join(self.root, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)

    # ---------------------------------------------------------------- listing
    def test_list_repository_reports_files_and_truncation(self):
        output = self.suite.list_repository()["output"]
        self.assertTrue(output["available"])
        self.assertIn("app.py", output["files"])
        self.assertIn("pkg/other.py", output["files"])
        limited = self.suite.list_repository(limit=1)["output"]
        self.assertEqual(1, len(limited["files"]))
        self.assertTrue(limited["truncated"])

    def test_list_repository_clamps_limit_to_a_positive_value(self):
        output = self.suite.list_repository(limit=0)["output"]
        self.assertEqual(1, len(output["files"]))

    def test_missing_root_degrades_to_unavailable(self):
        suite = RepositoryToolSuite("", "", parse_unified_diff(""))
        self.assertFalse(suite.repository_available)
        self.assertEqual([], suite.list_repository()["output"]["files"])
        with self.assertRaises(ValueError):
            suite.read_file("app.py")
        self.assertFalse(suite.run_scanners()["output"]["available"])
        self.assertFalse(
            suite.run_repository_checks("compile")["output"]["available"]
        )

    def test_non_directory_root_is_ignored(self):
        suite = RepositoryToolSuite(os.path.join(self.root, "missing"), "", parse_unified_diff(""))
        self.assertFalse(suite.repository_available)

    # ---------------------------------------------------------------- search
    def test_search_repository_is_case_insensitive_and_bounded(self):
        hits = self.suite.search_repository("hello")["output"]
        self.assertEqual(2, len(hits))
        self.assertEqual("notes.txt", hits[0]["path"])
        self.assertEqual(1, hits[0]["line"])
        self.assertEqual("hello world", hits[0]["content"])

        limited = self.suite.search_repository("hello", limit=1)["output"]
        self.assertEqual(1, len(limited))

    def test_search_repository_requires_a_query(self):
        with self.assertRaises(ValueError):
            self.suite.search_repository("   ")

    def test_search_diff_uses_one_based_line_numbers(self):
        hits = self.suite.search_diff("eval")["output"]
        self.assertEqual(1, len(hits))
        self.assertEqual(5, hits[0]["diff_line"])
        self.assertIn("eval", hits[0]["content"])
        self.assertEqual(1, len(self.suite.search_diff("EVAL", limit=1)["output"]))
        with self.assertRaises(ValueError):
            self.suite.search_diff("")

    # ------------------------------------------------------------ bounded read
    def test_read_file_clamps_ranges(self):
        output = self.suite.read_file("app.py", start_line=0, end_line=999)["output"]
        self.assertEqual(1, output["start_line"])
        self.assertEqual(10, output["end_line"])
        self.assertIn("def helper", output["content"])

        output = self.suite.read_file("app.py", start_line=4, end_line=5)["output"]
        self.assertEqual("def helper(value):\n    return os.path.basename(value)\n",
                         output["content"])

        output = self.suite.read_file("app.py", start_line=8, end_line=2)["output"]
        self.assertEqual(8, output["start_line"])
        self.assertEqual(8, output["end_line"])

    def test_changed_line_reports_hits_and_misses(self):
        found = self.suite.changed_line("app.py", 2)["output"]
        self.assertTrue(found["found"])
        self.assertEqual("value = eval(raw)", found["content"])
        missing = self.suite.changed_line("app.py", 1)["output"]
        self.assertFalse(missing["found"])

    # ---------------------------------------------------------------- symbols
    def test_symbol_finds_definitions_callers_and_callees(self):
        output = self.suite.symbol("helper")["output"]
        paths = {item["path"] for item in output["definitions"]}
        self.assertEqual({"app.py", "pkg/other.py"}, paths)
        self.assertTrue(all(item["kind"] == "FunctionDef" for item in output["definitions"]))
        callers = {item["path"] for item in output["callers"]}
        self.assertIn("app.py", callers)
        self.assertIn("pkg/other.py", callers)
        callees = {item["symbol"] for item in output["callees"]}
        self.assertIn("basename", callees)

    def test_symbol_reports_classes_and_skips_invalid_python(self):
        output = self.suite.symbol("Service")["output"]
        self.assertEqual(
            [{"path": "app.py", "line": 8, "kind": "ClassDef"}],
            output["definitions"],
        )

    def test_symbol_requires_a_name(self):
        with self.assertRaises(ValueError):
            self.suite.symbol("  ")

    def test_locate_tests_scores_path_tokens(self):
        by_path = self.suite.locate_tests(path="app.py")["output"]
        self.assertEqual("tests/test_app.py", by_path[0]["path"])
        self.assertGreaterEqual(by_path[0]["relevance"], 1)

        by_symbol = self.suite.locate_tests(symbol="helper")["output"]
        self.assertIn("tests/test_helper.py", {item["path"] for item in by_symbol})

        everything = self.suite.locate_tests()["output"]
        self.assertTrue(all("test" in item["path"] or "spec" in item["path"]
                            for item in everything))

    def test_read_project_controls_selects_dependency_and_ci_files(self):
        selected = {item["path"]: item["content"]
                    for item in self.suite.read_project_controls()["output"]}
        self.assertEqual('{"name": "demo"}\n', selected["package.json"])
        self.assertIn("name: ci", selected[".github/workflows/ci.yml"])

    # ------------------------------------------------------------- ast analysis
    def test_ast_analyze_reports_nodes_and_dangerous_calls(self):
        output = self.suite.ast_analyze("app.py")["output"]
        self.assertTrue(output["valid"])
        self.assertGreater(output["node_counts"]["FunctionDef"], 0)
        self.assertGreater(output["node_counts"]["ClassDef"], 0)
        self.assertEqual([], output["dangerous_calls"])

        self._write("danger.py", "value = eval(raw)\nrun(thing)\n")
        dangerous = self.suite.ast_analyze("danger.py")["output"]["dangerous_calls"]
        self.assertEqual({"eval", "run"}, {item["symbol"] for item in dangerous})

    def test_ast_analyze_reports_syntax_errors(self):
        output = self.suite.ast_analyze("pkg/broken.py")["output"]
        self.assertFalse(output["valid"])
        self.assertIn("error", output)

    def test_ast_analyze_marks_non_python_as_unavailable(self):
        output = self.suite.ast_analyze("notes.txt")["output"]
        self.assertIsNone(output["valid"])
        self.assertIn("Tree-sitter", output["note"])

    # -------------------------------------------------------------- git context
    def test_git_context_without_repository(self):
        output = self.suite.git_context("app.py")["output"]
        self.assertFalse(output["available"])

    def test_git_context_runs_fixed_commands(self):
        os.makedirs(os.path.join(self.root, ".git"), exist_ok=True)
        calls = []

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            return result(stdout="abc\t2024-01-01\tsubject\n")

        with patch.dict(os.environ, {"EVOAGENT_AUTH_SECRET": "top-secret"}), \
                patch("evoagent.agent.repository_tools.shutil.which", return_value="git"), \
                patch("evoagent.agent.repository_tools.subprocess.run", side_effect=fake_run):
            output = self.suite.git_context("app.py", line=5)["output"]
        self.assertTrue(output["available"])
        self.assertEqual(2, len(output["results"]))
        self.assertEqual(self.root, calls[0][1]["cwd"])
        self.assertNotIn("EVOAGENT_AUTH_SECRET", calls[0][1]["env"])
        self.assertEqual([
            "git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=",
            "-c", "core.pager=cat", "-c", "credential.helper=",
            "blame", "-L", "2,8", "--", "app.py",
        ], calls[1][0])

    def test_tool_version_parses_first_major_and_handles_failures(self):
        with patch("evoagent.agent.repository_tools.subprocess.run",
                   return_value=result(stdout="v9.2.1\n")):
            self.assertEqual(9, RepositoryToolSuite._tool_version("eslint"))
        with patch("evoagent.agent.repository_tools.subprocess.run",
                   return_value=result(stderr="node 8.0.0\n")):
            self.assertEqual(8, RepositoryToolSuite._tool_version("node"))
        with patch("evoagent.agent.repository_tools.subprocess.run",
                   return_value=result(stdout="no version\n")):
            self.assertIsNone(RepositoryToolSuite._tool_version("tool"))
        with patch("evoagent.agent.repository_tools.subprocess.run",
                   side_effect=OSError("missing")):
            self.assertIsNone(RepositoryToolSuite._tool_version("tool"))

    # ---------------------------------------------------------------- scanners
    def test_scanners_are_all_unavailable_when_not_installed(self):
        with patch("evoagent.agent.repository_tools.shutil.which", return_value=None):
            runs = self.suite.run_scanners()["output"]["runs"]
        self.assertEqual(
            {"semgrep", "bandit", "eslint", "mypy", "pyright"},
            {item["name"] for item in runs},
        )
        self.assertTrue(all(not item["available"] for item in runs))

    def test_semgrep_requires_a_trusted_local_rules_path(self):
        def which(name):
            return "semgrep" if name == "semgrep" else None

        with patch("evoagent.agent.repository_tools.shutil.which", side_effect=which), \
                patch.dict(os.environ, {}, clear=False):
            os.environ.pop("EVOAGENT_SEMGREP_CONFIG", None)
            runs = self.suite.run_scanners()["output"]["runs"]
        semgrep = next(item for item in runs if item["name"] == "semgrep")
        self.assertFalse(semgrep["available"])
        self.assertIn("EVOAGENT_SEMGREP_CONFIG", semgrep["reason"])

    def test_scanners_use_fixed_commands_and_supported_eslint_version(self):
        commands = []

        def which(name):
            return "C:/tools/%s.exe" % name

        def fake_run(command, **kwargs):
            commands.append(command)
            if command[-1] == "--version":
                return result(stdout="v9.0.0\n")
            return result(stdout='{"results": []}')

        with patch("evoagent.agent.repository_tools.shutil.which", side_effect=which), \
                patch("evoagent.agent.repository_tools.subprocess.run", side_effect=fake_run), \
                patch.dict(os.environ, {"EVOAGENT_SEMGREP_CONFIG": "rules.yml"}):
            runs = self.suite.run_scanners()["output"]["runs"]

        names = {item["name"] for item in runs}
        self.assertEqual({"semgrep", "bandit", "eslint", "mypy", "pyright"}, names)
        self.assertTrue(all(item["available"] for item in runs))
        flat = [" ".join(item) for item in commands]
        self.assertTrue(any("--config rules.yml" in item for item in flat))
        self.assertTrue(any("--no-config-lookup" in item for item in flat))
        self.assertTrue(any("--config-file" in item for item in flat))

    def test_eslint_v7_and_unknown_versions_are_rejected(self):
        def which(name):
            return "eslint" if name == "eslint" else None

        with patch("evoagent.agent.repository_tools.shutil.which", side_effect=which), \
                patch("evoagent.agent.repository_tools.subprocess.run",
                      return_value=result(stdout="v7.1.0\n")):
            runs = self.suite.run_scanners()["output"]["runs"]
        eslint = next(item for item in runs if item["name"] == "eslint")
        self.assertFalse(eslint["available"])
        self.assertIn("unsupported", eslint["reason"])

    def test_scanner_timeout_is_reported(self):
        def which(name):
            return "bandit" if name == "bandit" else None

        with patch("evoagent.agent.repository_tools.shutil.which", side_effect=which), \
                patch("evoagent.agent.repository_tools.subprocess.run",
                      side_effect=subprocess.TimeoutExpired("bandit", 90)):
            runs = self.suite.run_scanners()["output"]["runs"]
        bandit = next(item for item in runs if item["name"] == "bandit")
        self.assertTrue(bandit["timed_out"])

    # ------------------------------------------------------- repository checks
    def test_repository_checks_reject_unknown_kind(self):
        with self.assertRaises(ValueError):
            self.suite.run_repository_checks("deploy")

    def test_repository_checks_require_a_configured_test_command(self):
        output = self.suite.run_repository_checks("tests")["output"]
        self.assertFalse(output["available"])
        self.assertIn("administrator-configured", output["reason"])

    def test_compile_check_passes_and_fails_in_an_isolated_copy(self):
        with tempfile.TemporaryDirectory() as clean:
            with open(os.path.join(clean, "ok.py"), "w", encoding="utf-8") as handle:
                handle.write("value = 1\n")
            suite = RepositoryToolSuite(clean, "", parse_unified_diff(""))
            passed = suite.run_repository_checks("compile")["output"]
            self.assertEqual(0, passed["returncode"])
            self.assertTrue(passed["passed"])
            self.assertEqual("temporary-copy+restricted-environment", passed["isolation"])

            with open(os.path.join(clean, "bad.py"), "w", encoding="utf-8") as handle:
                handle.write("def broken(:\n")
            failed = suite.run_repository_checks("compile")["output"]
            self.assertFalse(failed["passed"])

    def test_configured_test_command_runs_in_the_isolated_copy(self):
        suite = RepositoryToolSuite(
            self.root, "", parse_unified_diff(""), test_command="python -c pass",
        )
        output = suite.run_repository_checks("tests")["output"]
        self.assertTrue(output["available"])
        self.assertTrue(output["passed"])

    def test_repository_checks_do_not_copy_sensitive_files(self):
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, "app.py"), "w", encoding="utf-8") as handle:
                handle.write("value = 1\n")
            with open(os.path.join(root, ".env"), "w", encoding="utf-8") as handle:
                handle.write("SECRET=do-not-copy\n")
            with open(os.path.join(root, ".env.example"), "w", encoding="utf-8") as handle:
                handle.write("SECRET=\n")
            suite = RepositoryToolSuite(
                root, "", parse_unified_diff(""),
                test_command=(
                    "python -c \"import os, sys; "
                    "sys.exit(0 if not os.path.exists('.env') "
                    "and os.path.exists('.env.example') else 1)\""
                ),
            )
            output = suite.run_repository_checks("tests")["output"]
            self.assertTrue(output["passed"], output)

    # ----------------------------------------------------------------- registry
    def test_registry_exposes_only_allowed_tools(self):
        registry = self.suite.registry("reviewer", allowed={"read_file", "search_diff"})
        self.assertEqual(["read_file", "search_diff"], registry.names())
        output = registry.invoke("search_diff", {"query": "eval"})["output"]
        self.assertEqual(1, len(output))

    def test_registry_lists_every_tool_by_default(self):
        registry = self.suite.registry("reviewer")
        self.assertEqual([
            "ast_analyze", "changed_line", "git_context", "list_repository",
            "locate_tests", "read_file", "read_project_controls",
            "run_repository_checks", "run_scanners", "search_diff",
            "search_repository", "symbol",
        ], registry.names())

    def test_registry_validates_tool_arguments(self):
        registry = self.suite.registry("reviewer", allowed={"read_file", "list_repository"})
        with self.assertRaises(ToolProtocolError):
            registry.invoke("read_file", {})
        with self.assertRaises(ToolProtocolError):
            registry.invoke("read_file", {"path": 1})
        with self.assertRaises(ToolProtocolError):
            registry.invoke("list_repository", {"limit": 0})
        with self.assertRaises(ToolProtocolError):
            registry.invoke("list_repository", {"limit": 2001})
        with self.assertRaises(ToolProtocolError):
            registry.invoke("list_repository", {"unknown": 1})
        with self.assertRaises(ToolProtocolError):
            registry.invoke("missing", {})

    def test_registry_records_success_and_failure_in_the_ledger(self):
        ledger = ExecutionLedger("reviewer")
        suite = RepositoryToolSuite(
            self.root, DIFF, parse_unified_diff(DIFF), ledger=ledger,
        )
        registry = suite.registry("reviewer", allowed={"read_file", "search_diff"})
        registry.invoke("search_diff", {"query": "eval"})
        with self.assertRaises(ValueError):
            registry.invoke("read_file", {"path": ".env"})
        self.assertEqual(2, len(ledger.tool_calls))
        self.assertTrue(ledger.tool_calls[0].ok)
        self.assertEqual("search_diff", ledger.tool_calls[0].tool)
        self.assertFalse(ledger.tool_calls[1].ok)
        self.assertIn("not readable", ledger.tool_calls[1].error)


class SensitivePathHelperTests(unittest.TestCase):
    def test_normalisation_blocks_windows_separators_and_relative_segments(self):
        for path in (
            r".ssh\id_rsa", r"nested\.aws\credentials", "a/../.env",
            ".git/config", "nested/key.pem",
        ):
            self.assertTrue(_sensitive_path(path), msg=path)

    def test_templates_and_similar_names_remain_allowed(self):
        for path in (
            ".env.example", ".env.sample", "config/.env.template",
            "docs/ssh_notes.txt", "gitsync/config", "src/credentials_helper.py",
        ):
            self.assertFalse(_sensitive_path(path), msg=path)


if __name__ == "__main__":
    unittest.main()
