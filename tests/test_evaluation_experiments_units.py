"""Unit tests for controlled evaluation experiments and helper gates."""
import json
import os
import random
import unittest
from unittest.mock import patch

from evoagent.agent.reviewers import Reviewer
from evoagent.agent.telemetry import ExecutionLedger
from evoagent.evaluation.experiments import (
    ACCURACY_METRICS, AccuracyExperimentSuite, ControlledExperimentClient,
    ControlledSkillReviewer, SkillEvolutionExperimentSuite,
    _artifact_sha256, prepare_controlled_experiment_cases,
)
from evoagent.evaluation.skill_evolution import SkillEvolutionEngine
from evoagent.evaluation.experiments import DEFAULT_CONTROLLED_DATASET
from evoagent.domain.diff_parser import parse_unified_diff


DIFF = "--- a/app.py\n+++ b/app.py\n@@ -0,0 +1 @@\n+result = eval(raw)\n"

NEEDS_CONTROLLED_DATASET = unittest.skipUnless(
    os.path.exists(DEFAULT_CONTROLLED_DATASET),
    "evaluation_data/pr_diff_100.jsonl is not present",
)


def real_case(case_id, split, repository):
    return {
        "id": case_id, "repository": repository, "pull_request": 1,
        "split": split, "source": {"kind": "public-github-pr"}, "diff": DIFF,
        "expected_findings": [{
            "path": "app.py", "start_line": 1, "end_line": 1,
            "cwe": "CWE-95", "severity": "critical", "should_comment": True,
        }],
    }


class PrepareCasesTests(unittest.TestCase):
    def _cases(self, repositories=10, source="offline-fixture"):
        cases = []
        for index in range(100):
            cases.append({
                "id": "c%d" % index,
                "repository": "org/repo-%02d" % (index % repositories),
                "source": {"kind": source},
                "expected_findings": [{"path": "a.py"}],
            })
        return cases

    def test_rejects_wrong_case_count(self):
        with self.assertRaisesRegex(ValueError, "100 cases"):
            prepare_controlled_experiment_cases(self._cases()[:99])

    def test_rejects_wrong_repository_count(self):
        with self.assertRaisesRegex(ValueError, "100 cases"):
            prepare_controlled_experiment_cases(self._cases(repositories=9))

    def test_rejects_non_fixture_provenance(self):
        with self.assertRaisesRegex(ValueError, "offline-fixture"):
            prepare_controlled_experiment_cases(self._cases(source="public-github-pr"))

    def test_adds_splits_and_should_comment_defaults_without_mutation(self):
        cases = self._cases()
        original = json.loads(json.dumps(cases))
        adapted = prepare_controlled_experiment_cases(cases)
        self.assertEqual({"train", "validation", "holdout"},
                         {item["split"] for item in adapted})
        self.assertTrue(all(
            finding["should_comment"] for item in adapted
            for finding in item["expected_findings"]
        ))
        self.assertEqual(original, cases)


class ControlledClientTests(unittest.TestCase):
    def setUp(self):
        self.client = ControlledExperimentClient()

    def test_single_reviewer_role_runs_local_rules(self):
        ledger = ExecutionLedger("single")
        result = self.client.complete_json(
            "single-reviewer", "", json.dumps({"unified_diff": DIFF}), ledger,
        )
        self.assertEqual(["SEC-EVAL"], [item["rule_id"] for item in result["findings"]])
        self.assertEqual(1, len(ledger.model_calls))
        self.assertEqual(0.0, ledger.model_calls[0].cost_usd)

    def test_lead_delegate_assess_and_finalize_phases(self):
        delegate = self.client.complete_json("lead", "", json.dumps({"task": json.dumps({
            "phase": "delegate",
            "requested_agent_skills": ["skill-a"],
            "changed_files": ["app.py"],
            "enabled_workers": ["security", "critic"],
        })}), None)
        self.assertEqual("final", delegate["action"])
        self.assertEqual(
            ["security-1", "critic-1"],
            [item["assignment_id"] for item in delegate["delegations"]],
        )
        self.assertEqual(["skill-a"], delegate["delegations"][0]["skills"])

        assess = self.client.complete_json("lead", "", json.dumps({"task": json.dumps({
            "phase": "assess-workers",
        })}), None)
        self.assertEqual([], assess["revision_requests"])

        finalize = self.client.complete_json("lead", "", json.dumps({"task": json.dumps({
            "phase": "finalize",
            "candidate_findings": ["a", "b", "c"],
            "critic_decisions": [
                {"finding_index": 0, "accepted": False},
                {"finding_index": 2, "accepted": True},
            ],
        })}), None)
        self.assertEqual([1, 2], finalize["accepted_finding_indices"])

    def test_worker_roles_return_no_findings(self):
        for role in ("security", "correctness-reliability"):
            result = self.client.complete_json(role, "", json.dumps({"task": "{}"}), None)
            self.assertEqual([], result["findings"])

    def test_critic_rejects_benign_fixture_matches(self):
        result = self.client.complete_json("critic", "", json.dumps({"task": json.dumps({
            "candidates": [
                {"evidence": "token = \"test-placeholder\""},
                {"evidence": "checksum = hashlib.md5(b\"fixture-id\")"},
                {"evidence": "result = eval(raw)"},
            ],
        })}), None)
        self.assertEqual(
            [False, False, True],
            [item["accepted"] for item in result["decisions"]],
        )
        self.assertEqual(["controlled benign fixture"], result["decisions"][0]["objections"])

    def test_unknown_role_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unsupported controlled role"):
            self.client.complete_json("bogus", "", json.dumps({"task": "{}"}), None)

    def test_artifact_hash_is_stable_and_compact(self):
        artifact = {"b": 2, "a": 1}
        self.assertEqual(_artifact_sha256(artifact), _artifact_sha256({"a": 1, "b": 2}))
        self.assertEqual(64, len(_artifact_sha256(artifact)))


@NEEDS_CONTROLLED_DATASET
class ControlledSkillReviewerTests(unittest.TestCase):
    def _artifact(self, rules):
        blocks = "".join(
            "<!-- evoagent:learned:%s:start -->\n## %s\n"
            "<!-- evoagent:learned:%s:end -->\n" % (rule, rule, rule)
            for rule in rules
        )
        return {
            "name": "evolved-review",
            "files": {"SKILL.md": (
                "---\nname: evolved-review\n"
                'description: "Replay validated review guidance."\n---\n\n# G\n\n' + blocks
            )},
        }

    def test_learned_rules_add_findings_only_when_the_pattern_matches(self):
        reviewer = ControlledSkillReviewer(self._artifact(["SEC-WEAK-HASH"]))
        diff = "--- a/app.py\n+++ b/app.py\n@@ -0,0 +1 @@\n+digest = hashlib.md5(value)\n"
        findings = reviewer.review(diff, parse_unified_diff(diff))
        self.assertEqual(["SEC-WEAK-HASH"], [item.rule_id for item in findings])
        self.assertEqual("controlled-offline-skill-policy", findings[0].source)

        clean = "--- a/app.py\n+++ b/app.py\n@@ -0,0 +1 @@\n+value = 1\n"
        self.assertEqual([], reviewer.review(clean, parse_unified_diff(clean)))

    def test_local_rules_still_apply_and_duplicates_are_suppressed(self):
        reviewer = ControlledSkillReviewer(self._artifact(["SEC-EVAL"]))
        findings = reviewer.review(DIFF, parse_unified_diff(DIFF))
        self.assertEqual(["SEC-EVAL"], [item.rule_id for item in findings])


class AccuracySuiteTests(unittest.TestCase):
    def test_run_real_reports_readiness_metrics_and_no_repair(self):
        suite = AccuracyExperimentSuite(minimum_real_cases=3)
        cases = [
            real_case("train-1", "train", "org/train"),
            real_case("validation-1", "validation", "org/validation"),
            real_case("holdout-1", "holdout", "org/holdout"),
        ]
        report = suite.run_real(cases, AccuracyExperimentSuite.controlled_reviewer())
        self.assertTrue(report["dataset_readiness"]["ready"])
        self.assertTrue(report["production_claim_allowed"])
        self.assertEqual("not-configured", report["repair_evaluation"])
        self.assertEqual(set(ACCURACY_METRICS), set(report["metrics"]))
        self.assertIn("f1", report["report"]["metrics"])

    def test_run_real_blocks_incomplete_datasets(self):
        suite = AccuracyExperimentSuite(minimum_real_cases=300)
        with self.assertRaisesRegex(ValueError, "readiness gates"):
            suite.run_real(
                [real_case("c", "train", "org/train")],
                AccuracyExperimentSuite.controlled_reviewer(),
            )

    def test_run_requires_a_reviewer_when_real_cases_are_supplied(self):
        suite = AccuracyExperimentSuite(minimum_real_cases=1)
        with patch.object(suite, "run_controlled", return_value={"ok": True}):
            with self.assertRaisesRegex(ValueError, "real_reviewer"):
                suite.run(real_cases=[real_case("c", "train", "org/r")])

    def test_run_combines_controlled_and_real_reports(self):
        suite = AccuracyExperimentSuite(minimum_real_cases=3)
        cases = [
            real_case("train-1", "train", "org/train"),
            real_case("validation-1", "validation", "org/validation"),
            real_case("holdout-1", "holdout", "org/holdout"),
        ]
        with patch.object(suite, "run_controlled", return_value={"contract": True}):
            report = suite.run(
                real_cases=cases,
                real_reviewer=AccuracyExperimentSuite.controlled_reviewer(),
            )
        self.assertEqual("code-review-accuracy", report["experiment"])
        self.assertEqual({"contract": True}, report["controlled"])
        self.assertTrue(report["real"]["production_claim_allowed"])
        self.assertEqual(list(ACCURACY_METRICS), report["metric_contract"])


class _EmptyReviewer(Reviewer):
    name = "empty"

    def review(self, diff, parsed):
        return []

    def evaluation_execution(self):
        return {
            "llm_calls": 1, "input_tokens": 10, "output_tokens": 5,
            "total_tokens": 15, "duration_ms": 2, "cost_usd": 0.001,
        }


class _BoomReviewer(_EmptyReviewer):
    def review_case(self, case, parsed):
        raise RuntimeError("review failed")


class _Case:
    split = "train"


class ExperimentHelperTests(unittest.TestCase):
    def setUp(self):
        self.suite = SkillEvolutionExperimentSuite(
            lambda artifact: _EmptyReviewer(), minimum_real_cases=1,
            max_rounds=1, bootstrap_iterations=200,
        )

    def test_split_and_line_evidence(self):
        cases = [{"split": "train"}, {"split": "validation"}]
        self.assertEqual([{"split": "train"}], self.suite._split(cases, "train"))
        case = real_case("c", "validation", "org/r")
        truth = {
            "path": "app.py", "start_line": 1, "end_line": 1,
            "cwe": "CWE-95", "severity": "critical",
        }
        self.assertEqual("result = eval(raw)", self.suite._line_evidence(case, truth))
        missing = dict(truth, path="other.py")
        self.assertIn("confirmed defect", self.suite._line_evidence(case, missing))

    def test_collect_feedback_reports_misses_and_execution_totals(self):
        case = real_case("c1", "train", "org/r")
        collected = self.suite._collect_feedback({}, [case])
        self.assertEqual(1, collected["missed_findings"])
        self.assertEqual(1, collected["cases"])
        self.assertEqual([], collected["errors"])
        self.assertEqual(1, collected["execution"]["llm_calls"])
        self.assertEqual(15, collected["execution"]["total_tokens"])
        self.assertEqual(0.001, collected["execution"]["cost_usd"])
        self.assertEqual("CWE-95", collected["feedback"][0]["payload"]["finding"]["rule_id"])

    def test_collect_feedback_records_reviewer_errors(self):
        suite = SkillEvolutionExperimentSuite(
            lambda artifact: _BoomReviewer(), minimum_real_cases=1,
        )
        collected = suite._collect_feedback({}, [real_case("c1", "train", "org/r")])
        self.assertEqual(1, len(collected["errors"]))
        self.assertEqual("review failed", collected["errors"][0]["error"])
        self.assertEqual([], collected["feedback"])

    def test_random_feedback_is_deterministic_and_benign(self):
        first = self.suite._random_feedback(3, 1, random.Random(7))
        second = self.suite._random_feedback(3, 1, random.Random(7))
        self.assertEqual(first, second)
        self.assertEqual(3, len(first))
        self.assertTrue(all(
            item["payload"]["finding"]["rule_id"].startswith("RANDOM-CONTROL-01")
            for item in first
        ))
        self.assertTrue(all(
            "\n" not in item["payload"]["finding"]["evidence"] for item in first
        ))

    def test_validation_and_holdout_gates(self):
        baseline = {"metrics": {
            "f1": 0.5, "precision": 0.5, "recall": 0.5,
            "high_risk_recall": 0.5, "severity_accuracy": 0.5, "clean_accuracy": 0.5,
            "safe_fix_rate": 0.5, "e2e_security_fix_rate": 0.5,
            "execution_success_rate": 1.0,
        }}
        candidate = {"metrics": {**baseline["metrics"], "f1": 0.52}}
        gate = self.suite._validation_gate(baseline, candidate)
        self.assertTrue(gate["passed"])
        self.assertEqual(0.02, gate["f1_improvement"])

        regressed = {"metrics": {**candidate["metrics"], "precision": 0.49}}
        self.assertFalse(self.suite._validation_gate(baseline, regressed)["passed"])
        self.assertIn("precision",
                      self.suite._holdout_gate(baseline, regressed)["protected_metric_regressions"])
        self.assertTrue(self.suite._holdout_gate(baseline, candidate)["passed"])

    def test_aggregate_reports_mean_and_none_for_missing_metrics(self):
        def run(value):
            metrics = {name: value for name in ACCURACY_METRICS}
            metrics["average_cost_usd_per_pr"] = None
            return {"arms": {
                arm: {"metrics": metrics}
                for arm in ("static-skill", "evolved-skill", "random-feedback-skill")
            }}

        aggregate = self.suite._aggregate([run(0.2), run(0.4)])
        self.assertEqual(0.3, aggregate["static-skill"]["f1"]["mean"])
        self.assertEqual(2, aggregate["static-skill"]["f1"]["runs"])
        self.assertIsNone(aggregate["static-skill"]["average_cost_usd_per_pr"])

    def test_run_blocks_incomplete_real_datasets(self):
        suite = SkillEvolutionExperimentSuite(
            lambda artifact: _EmptyReviewer(), minimum_real_cases=300,
        )
        with self.assertRaisesRegex(ValueError, "readiness gates"):
            suite.run(
                [real_case("c", "train", "org/r")],
                SkillEvolutionEngine.empty_artifact("evolved-review"),
            )


if __name__ == "__main__":
    unittest.main()
