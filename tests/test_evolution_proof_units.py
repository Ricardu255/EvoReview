"""Unit and small end-to-end tests for the prompt-evolution proof module."""
import json
import os
import tempfile
import unittest

from evoagent.adapters.stores.sqlite.store import TaskStore
from evoagent.evaluation.evolution_proof import (
    FOCUS_RULE, PromptPolicyReviewer, _collect_feedback, _evolution_case,
    _metric_delta, _prompt_sha256, load_prompt_evolution_cases,
    render_markdown, run_prompt_evolution_proof, write_jsonl, write_report,
)
from evoagent.domain.diff_parser import parse_unified_diff


def diff_for(line, path="app.py"):
    return "--- a/%s\n+++ b/%s\n@@ -0,0 +1 @@\n+%s\n" % (path, path, line)


def dataset_case(case_id, split, line, expected_rule=None, path="app.py", cwe="CWE-328"):
    expected = []
    if expected_rule:
        expected.append({
            "path": path, "start_line": 1, "end_line": 1,
            "cwe": cwe, "rule_id": expected_rule, "severity": "medium",
        })
    return {
        "id": case_id,
        "repository": "org/repo-%s" % split,
        "pull_request": 1,
        "split": split,
        "source": {"kind": "public-github-pr"},
        "diff": diff_for(line, path),
        "expected_findings": expected,
    }


class PromptEvolutionProofUnitTests(unittest.TestCase):
    def test_focus_rule_marker_requires_an_upper_case_identifier(self):
        self.assertEqual(
            {"SEC-WEAK-HASH", "REL_X"},
            set(FOCUS_RULE.findall(
                "[focus-rule:SEC-WEAK-HASH] [focus-rule:REL_X] [focus-rule:lower]"
            )),
        )
        self.assertEqual(set(), set(FOCUS_RULE.findall("[focus-rule:x]")))

    def test_prompt_policy_reviewer_always_runs_local_rules(self):
        reviewer = PromptPolicyReviewer("plain prompt")
        diff = diff_for("result = eval(raw)")
        findings = reviewer.review(diff, parse_unified_diff(diff))
        self.assertEqual({"SEC-EVAL"}, {item.rule_id for item in findings})
        self.assertEqual(set(), reviewer.focus_rules)

    def test_focus_rule_enables_only_the_selected_supplemental_rule(self):
        reviewer = PromptPolicyReviewer("[focus-rule:SEC-WEAK-HASH]")
        diff = diff_for("digest = hashlib.md5(value).hexdigest()\n"
                        "return open(base / user_path).read()")
        # Two added lines are needed for two findings.
        diff = (
            "--- a/app.py\n+++ b/app.py\n@@ -0,0 +1,2 @@\n"
            "+digest = hashlib.md5(value).hexdigest()\n"
            "+return open(base / user_path).read()\n"
        )
        findings = reviewer.review(diff, parse_unified_diff(diff))
        self.assertEqual({"SEC-WEAK-HASH"}, {item.rule_id for item in findings})

    def test_evolution_case_converts_labels(self):
        case = dataset_case("c1", "validation", "digest = hashlib.md5(v)", "SEC-WEAK-HASH")
        converted = _evolution_case(case)
        self.assertEqual("proof-c1", converted["name"])
        self.assertEqual("validation", converted["split"])
        self.assertEqual([{
            "path": "app.py", "line": 1, "rule_id": "SEC-WEAK-HASH",
            "min_severity": "medium",
        }], converted["expected"])
        self.assertEqual("public-github-pr", converted["source"])

    def test_evolution_case_falls_back_to_cwe_as_the_rule_id(self):
        case = dataset_case("c1", "validation", "x = 1", "SEC-WEAK-HASH")
        case["expected_findings"][0].pop("rule_id")
        self.assertEqual(
            "CWE-328", _evolution_case(case)["expected"][0]["rule_id"],
        )

    def test_metric_delta_covers_every_public_metric_and_rounds(self):
        baseline = {name: 0.5 for name in (
            "score", "precision", "recall", "f1", "severity_accuracy",
            "high_severity_recall", "clean_accuracy", "success_rate",
        )}
        candidate = {name: 0.666666 for name in baseline}
        delta = _metric_delta(candidate, baseline)
        self.assertEqual(8, len(delta))
        self.assertEqual(0.1667, delta["score"])

    def test_prompt_hash_is_stable(self):
        self.assertEqual(_prompt_sha256("abc"), _prompt_sha256("abc"))
        self.assertNotEqual(_prompt_sha256("abc"), _prompt_sha256("abd"))
        self.assertEqual(64, len(_prompt_sha256("abc")))

    def test_write_and_load_jsonl_round_trip(self):
        with tempfile.TemporaryDirectory() as temp:
            path = os.path.join(temp, "nested", "cases.jsonl")
            cases = [
                dataset_case("c1", "validation", "x = 1"),
                dataset_case("c2", "holdout", "y = 2"),
            ]
            write_jsonl(cases, path)
            self.assertEqual(cases, load_prompt_evolution_cases(path))
            with open(path, encoding="utf-8") as handle:
                self.assertTrue(handle.read().endswith("\n"))

    def test_collect_feedback_records_only_unmatched_validation_cases(self):
        with tempfile.TemporaryDirectory() as temp:
            store = TaskStore(os.path.join(temp, "feedback.db"))
            cases = [
                dataset_case("miss", "validation", "value = int(raw)",
                             "SEC-WEAK-HASH"),
                dataset_case("hit", "validation", "result = eval(raw)", "SEC-EVAL",
                             cwe="CWE-95"),
                dataset_case("train", "train", "nothing()", "SEC-WEAK-HASH"),
            ]
            feedback = _collect_feedback(store, cases, "plain prompt")
            self.assertEqual(1, len(feedback))
            self.assertEqual("SEC-WEAK-HASH", feedback[0]["finding"]["rule_id"])
            failures = store.list_failure_cases(False, 500)
            self.assertEqual(1, len(failures))
            self.assertEqual("missed_issue", failures[0]["category"])


class PromptEvolutionProofEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dataset = os.path.join(self.temp.name, "cases.jsonl")
        self.database = os.path.join(self.temp.name, "proof.db")
        cases = [
            dataset_case("v-pos-1", "validation",
                         "digest = hashlib.md5(value).hexdigest()", "SEC-WEAK-HASH"),
            dataset_case("v-pos-2", "validation",
                         "other = hashlib.md5(raw).hexdigest()", "SEC-WEAK-HASH"),
            dataset_case("v-clean-1", "validation", "value = int(raw)"),
            dataset_case("v-clean-2", "validation", "limit = min(value, 10)"),
            dataset_case("h-pos-1", "holdout",
                         "digest = hashlib.md5(input).hexdigest()", "SEC-WEAK-HASH"),
            dataset_case("h-clean-1", "holdout", "value = str(raw)"),
        ]
        write_jsonl(cases, self.dataset)

    def test_full_proof_activates_a_prompt_version_and_renders_reports(self):
        report = run_prompt_evolution_proof(self.dataset, self.database)

        self.assertEqual("activated", report["evolution_run"]["decision"])
        self.assertTrue(report["release_gate"]["quantitative_passed"])
        self.assertTrue(report["release_gate"]["production_data_provenance_passed"])
        self.assertTrue(report["release_gate"]["production_activation_allowed"])
        self.assertEqual(6, report["dataset"]["cases"])
        self.assertEqual(4, report["dataset"]["validation_cases"])
        self.assertEqual(2, report["dataset"]["holdout_cases"])
        self.assertEqual(2, report["feedback"]["missed_findings"])
        self.assertEqual(["SEC-WEAK-HASH"], report["feedback"]["learned_rule_ids"])
        self.assertEqual(2, report["feedback"]["resolved_after_activation"])
        self.assertGreater(report["validation"]["candidate"]["recall"],
                           report["validation"]["baseline"]["recall"])
        self.assertGreaterEqual(report["validation"]["delta"]["score"], 0.02)
        self.assertGreaterEqual(len(report["versions"]), 2)
        self.assertTrue(any(item["active"] for item in report["versions"]))

        markdown = render_markdown(report)
        self.assertIn("EvoReview 提示词版本进化回放证明", markdown)
        self.assertIn("`activated`", markdown)
        self.assertIn("生产激活：`ALLOWED`", markdown)
        self.assertIn("SEC-WEAK-HASH", markdown)
        self.assertIn(report["dataset"]["sha256"], markdown)

        paths = write_report(report, os.path.join(self.temp.name, "out"))
        self.assertTrue(os.path.exists(paths["json"]))
        self.assertTrue(os.path.exists(paths["markdown"]))
        with open(paths["json"], encoding="utf-8") as handle:
            self.assertEqual(report, json.load(handle))
        with open(paths["markdown"], encoding="utf-8") as handle:
            self.assertIn("提示词版本进化", handle.read())

    def test_render_markdown_marks_blocked_release_gates(self):
        report = run_prompt_evolution_proof(self.dataset, self.database)
        report["release_gate"] = {
            "quantitative_passed": False,
            "production_data_provenance_passed": False,
            "production_activation_allowed": False,
        }
        report["evolution_run"]["decision"] = "deferred"
        markdown = render_markdown(report)
        self.assertIn("数值门禁：`FAIL`", markdown)
        self.assertIn("生产来源门禁：`FAIL`", markdown)
        self.assertIn("生产激活：`BLOCKED`", markdown)


if __name__ == "__main__":
    unittest.main()
