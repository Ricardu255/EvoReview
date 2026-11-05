import os
import tempfile
import unittest

from evoagent.evaluation.experiments import (
    DEFAULT_CONTROLLED_DATASET,
    load_controlled_pr_cases,
)
from evoagent.evaluation.evolution import RegressionEvaluator
from evoagent.evaluation.harness import (
    EndToEndEvaluationHarness,
    dataset_fingerprint,
    load_jsonl,
    one_to_one_match,
)
from evoagent.agent.reviewers import Reviewer
from evoagent.domain.diff_parser import parse_unified_diff
from evoagent.domain.finding_identity import canonical_identity, normalized_identity
from evoagent.domain.models import Finding, Severity


# The 100-case controlled fixture is tracked; tests that read it skip only
# when the corpus is missing from a partial checkout.
NEEDS_CONTROLLED_DATASET = unittest.skipUnless(
    os.path.exists(DEFAULT_CONTROLLED_DATASET),
    "evaluation_data/pr_diff_100.jsonl is missing",
)


class EndToEndEvaluationTests(unittest.TestCase):
    @NEEDS_CONTROLLED_DATASET
    def test_generated_dataset_has_repository_level_split_and_expected_counts(self):
        cases = load_controlled_pr_cases()
        self.assertEqual(100, len(cases))
        self.assertEqual(40, sum(bool(item["expected_findings"]) for item in cases))
        self.assertEqual(60, sum(not item["expected_findings"] for item in cases))
        validation_repos = {
            item["repository"] for item in cases if item["split"] == "validation"
        }
        holdout_repos = {
            item["repository"] for item in cases if item["split"] == "holdout"
        }
        self.assertEqual(8, len(validation_repos))
        self.assertEqual(2, len(holdout_repos))
        self.assertFalse(validation_repos & holdout_repos)
        self.assertEqual(
            {"offline-fixture"},
            {item["source"]["kind"] for item in cases},
        )

    def test_normalized_identity_keeps_strict_identity_separate(self):
        self.assertEqual("CWE-396", canonical_identity("", "CWE-396"))
        self.assertEqual("CWE-703", normalized_identity("", "CWE-396"))
        self.assertEqual("CWE-330", normalized_identity("", "CWE-338"))
        self.assertEqual("CWE-798", normalized_identity("", "CWE-259"))
        self.assertEqual("CWE-328", normalized_identity("", "CWE-327"))

    def test_normalized_metrics_can_recover_a_sibling_cwe(self):
        class AliasReviewer(Reviewer):
            name = "alias-reviewer"

            def review(self, diff, parsed):
                line = parsed.added_lines[0]
                return [Finding(
                    "broad-exception-caught", Severity.MEDIUM, "Broad catch",
                    "Generic exception handling.", line.path, line.line,
                    line.content, "Use a narrow exception.", "Add a test.",
                    0.9, cwe="CWE-396",
                )]

        diff = (
            "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n"
            "-old\n+except Exception: pass\n"
        )
        case = {
            "id": "alias-case", "repository": "org/repo", "pull_request": 1,
            "split": "validation", "diff": diff,
            "expected_findings": [{
                "path": "app.py", "start_line": 1, "end_line": 1,
                "cwe": "CWE-703", "rule_id": "REL-EMPTY-EXCEPT",
                "severity": "medium", "should_comment": True,
            }],
        }
        report = EndToEndEvaluationHarness().run(AliasReviewer(), [case])
        self.assertEqual(0.0, report["metrics"]["f1"])
        self.assertEqual(1.0, report["metrics"]["f1_normalized"])
        self.assertEqual(1.0, report["metrics"]["clean_accuracy_normalized"])

    def test_one_to_one_matching_counts_duplicate_prediction_once(self):
        expected = [{
            "path": "src/a.py", "start_line": 10, "end_line": 12,
            "cwe": "CWE-95", "severity": "critical",
        }]
        predicted = [
            Finding(
                "SEC-EVAL", Severity.CRITICAL, "a", "long enough explanation",
                "src/a.py", line, "eval(x)", "replace eval safely",
                "add malicious input test", 0.9,
            )
            for line in (10, 11)
        ]
        matches = one_to_one_match(expected, predicted)
        self.assertEqual(1, len(matches))

    def test_one_to_one_matching_uses_explicit_cwe_not_random_rule_id(self):
        expected = [{
            "path": "src/a.py", "start_line": 10, "end_line": 10,
            "cwe": "CWE-95", "severity": "critical",
        }]
        predicted = [Finding(
            "MODEL-GENERATED-LABEL", Severity.CRITICAL, "a",
            "long enough explanation", "src/a.py", 10, "eval(x)",
            "replace eval safely", "add malicious input test", 0.9,
            cwe="CWE-95",
        )]
        self.assertEqual(1, len(one_to_one_match(expected, predicted)))

    def test_one_to_one_matching_rejects_unknown_rule_without_cwe(self):
        expected = [{
            "path": "src/a.py", "start_line": 10, "end_line": 10,
            "cwe": "CWE-95", "severity": "critical",
        }]
        predicted = [Finding(
            "MODEL-GENERATED-LABEL", Severity.CRITICAL, "a",
            "long enough explanation", "src/a.py", 10, "eval(x)",
            "replace eval safely", "add malicious input test", 0.9,
        )]
        self.assertEqual([], one_to_one_match(expected, predicted))

    def test_prompt_replay_accepts_model_label_when_cwe_is_canonical(self):
        case = {
            "name": "canonical-cwe",
            "diff": "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+eval(data)\n",
            "expected": [{
                "path": "a.py", "line": 1, "rule_id": "SEC-EVAL",
                "min_severity": "high",
            }],
        }

        class Reviewer:
            name = "test"

            def review(self, _diff, parsed):
                line = parsed.added_lines[0]
                return [Finding(
                    "MODEL-LABEL", Severity.CRITICAL, "x", "x",
                    line.path, line.line, line.content, "fix", "test", 0.9,
                    cwe="CWE-95",
                )]

        report = RegressionEvaluator(lambda _prompt: Reviewer()).run("p", [case])
        self.assertEqual(1, report["case_results"][0]["tp"])

    @NEEDS_CONTROLLED_DATASET
    def test_dataset_round_trip_has_stable_fingerprint(self):
        cases = load_controlled_pr_cases()
        handle, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(handle)
        try:
            import json
            with open(path, "w", encoding="utf-8") as output:
                for case in cases:
                    output.write(json.dumps(case, ensure_ascii=False) + "\n")
            loaded = load_jsonl(path)
            self.assertEqual(dataset_fingerprint(cases), dataset_fingerprint(loaded))
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
