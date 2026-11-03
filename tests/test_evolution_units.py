"""Unit tests for Prompt EvolutionEngine validation and gating helpers."""
import unittest

from evoagent.evaluation.evolution import (
    DEFAULT_PROMPT, EvolutionEngine, RegressionEvaluator,
)
from evoagent.domain.diff_parser import parse_unified_diff
from evoagent.agent.reviewers import LocalRuleReviewer


DIFF = "--- a/app.py\n+++ b/app.py\n@@ -0,0 +1 @@\n+result = eval(raw)\n"
EXPECTED = [{"path": "app.py", "line": 1, "min_severity": "high"}]


class FakeStore:
    def __init__(self):
        self.failure_cases = []
        self.active = None
        self.runs = []
        self.updates = []
        self.resolved = []
        self.versions = []
        self.evaluation_calls = []
        self.activated = []

    def list_evaluation_cases(self, split, include, limit, tenant_id=None):
        return []

    def list_failure_cases(self, unresolved_only, limit, tenant_id=None):
        return list(self.failure_cases)

    def get_active_skill_version(self, skill_name, tenant_id):
        return self.active

    def save_skill_version(self, skill_name, prompt, score, active, tenant_id):
        version = {
            "skill_name": skill_name, "version": len(self.versions) + 1,
            "score": score, "active": active, "prompt": prompt,
            "tenant_id": tenant_id,
        }
        self.versions.append(version)
        return version

    def save_evolution_run(self, run):
        self.runs.append(run)

    def list_evolution_runs(self, limit=50, tenant_id=None):
        return list(reversed(self.runs))[:limit]

    def update_evolution_run(self, run_id, decision, metrics, tenant_id=None):
        self.updates.append((run_id, decision, metrics, tenant_id))

    def resolve_failure_cases(self, ids):
        self.resolved.extend(ids)

    def activate_skill_version(self, skill_name, version, tenant_id):
        self.activated.append((skill_name, version, tenant_id))
        return version == 1

    def save_evaluation_case(self, *args):
        self.evaluation_calls.append(args)
        return {"name": args[0], "split": args[1]}


class ValidateCaseTests(unittest.TestCase):
    def assert_invalid(self, **overrides):
        values = {"name": "case", "diff": DIFF, "expected": EXPECTED, "split": "validation"}
        values.update(overrides)
        with self.assertRaises(ValueError, msg=overrides):
            EvolutionEngine.validate_case(**values)

    def test_accepts_a_valid_case(self):
        EvolutionEngine.validate_case("case", DIFF, EXPECTED)

    def test_rejects_invalid_names_splits_and_oversized_diffs(self):
        self.assert_invalid(name="")
        self.assert_invalid(name="x" * 121)
        self.assert_invalid(split="test")
        self.assert_invalid(diff="+" + "x" * (1024 * 1024))

    def test_rejects_diffs_without_added_lines(self):
        self.assert_invalid(diff="--- a/app.py\n+++ b/app.py\n")

    def test_rejects_malformed_expected_lists(self):
        self.assert_invalid(expected="not-a-list")
        self.assert_invalid(expected=[{}] * 101)
        self.assert_invalid(expected=["not-a-dict"])

    def test_rejects_invalid_expected_locations(self):
        self.assert_invalid(expected=[{"path": "app.py", "line": "x"}])
        self.assert_invalid(expected=[{"path": "app.py", "line": 2, "end_line": 1}])
        self.assert_invalid(expected=[{"path": "other.py", "line": 1}])
        self.assert_invalid(expected=[{"path": "app.py", "line": 1, "min_severity": "urgent"}])

    def test_rejects_invalid_rule_ids_and_cwes(self):
        self.assert_invalid(expected=[{"path": "app.py", "line": 1, "rule_id": ""}])
        self.assert_invalid(expected=[{"path": "app.py", "line": 1, "rule_id": "X" * 81}])
        self.assert_invalid(expected=[{"path": "app.py", "line": 1, "cwe": "not-a-cwe"}])

    def test_rejects_duplicate_expected_findings(self):
        self.assert_invalid(expected=[
            {"path": "app.py", "line": 1}, {"path": "app.py", "line": 1},
        ])

    def test_add_case_strips_and_truncates_metadata(self):
        store = FakeStore()
        engine = EvolutionEngine(store, seed_defaults=False)
        result = engine.add_evaluation_case(
            "  case  ", DIFF, EXPECTED, " VALIDATION ", "s" * 200,
        )
        self.assertEqual("case", result["name"])
        self.assertEqual("validation", result["split"])
        self.assertEqual(120, len(store.evaluation_calls[0][4]))


class SafetyAndStatusTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()
        self.engine = EvolutionEngine(self.store, seed_defaults=False)

    def test_safety_gate_rejects_forbidden_instructions(self):
        result = self.engine.safety_evaluate("ignore previous diff severity fix test json")
        self.assertFalse(result["safety_passed"])
        self.assertEqual(1.0, result["completeness"])

    def test_safety_gate_rejects_empty_and_oversized_prompts(self):
        self.assertFalse(self.engine.safety_evaluate("")["safety_passed"])
        self.assertFalse(self.engine.safety_evaluate("x" * 12001)["safety_passed"])

    def test_completeness_lists_missing_terms(self):
        result = self.engine.safety_evaluate("review diff only")
        self.assertLess(result["completeness"], 1.0)
        self.assertIn("json", result["missing_terms"])

    def test_status_reports_readiness(self):
        status = self.engine.status()
        self.assertFalse(status["model_configured"])
        self.assertFalse(status["ready"])
        self.assertEqual(0, status["validation_cases"])
        self.assertEqual(64, len(status["validation_dataset_fingerprint"]))

        engine = EvolutionEngine(self.store, reviewer_factory=LocalRuleReviewer,
                                 seed_defaults=False, min_cases=0, min_holdout_cases=0)
        self.assertTrue(engine.status()["ready"])

    def test_for_tenant_reuses_self_and_scopes_new_engines(self):
        engine = EvolutionEngine(self.store, seed_defaults=True)
        self.assertIs(engine, engine.for_tenant("default"))
        before = len(self.store.evaluation_calls)
        scoped = engine.for_tenant("tenant-b")
        self.assertEqual("tenant-b", scoped.tenant_id)
        self.assertEqual(before * 2, len(self.store.evaluation_calls))

    def test_propose_requires_a_valid_skill_name(self):
        for name in ("", "   ", "x" * 121):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    self.engine.propose(name, "prompt")

    def test_rollback_delegates_to_the_store(self):
        self.assertTrue(self.engine.rollback("llm-review", 1))
        self.assertFalse(self.engine.rollback("llm-review", 2))


class AutoProposeTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()

    def test_defers_when_there_is_no_feedback(self):
        result = EvolutionEngine(self.store, seed_defaults=False).auto_propose("llm-review")
        self.assertEqual("deferred", result["decision"])
        self.assertIsNone(result["run_id"])
        self.assertEqual(0, result["failure_cases_used"])

    def test_ignores_unsupported_or_invalid_rule_feedback(self):
        self.store.failure_cases = [
            {"id": 1, "category": "missed_issue",
             "payload": {"finding": {"rule_id": "lower-case"}}},
            {"id": 2, "category": "unknown", "payload": {}},
        ]
        result = EvolutionEngine(self.store, seed_defaults=False).auto_propose("llm-review")
        self.assertEqual("deferred", result["decision"])
        self.assertEqual([], result["learned_rule_ids"])
        self.assertEqual({"missed_issue": 1, "unknown": 1}, result["learned_categories"])

    def test_learns_a_focus_rule_and_proposes_a_candidate(self):
        self.store.failure_cases = [
            {"id": 1, "category": "missed_issue",
             "payload": {"finding": {"rule_id": "SEC-WEAK-HASH"}}},
            {"id": 2, "category": "false_positive", "payload": {}},
        ]
        result = EvolutionEngine(self.store, seed_defaults=False).auto_propose("llm-review")
        self.assertEqual(["SEC-WEAK-HASH"], result["learned_rule_ids"])
        self.assertIn("focus-rule:SEC-WEAK-HASH", self.store.versions[-1]["prompt"])
        # No reviewer is configured, so the candidate is saved but deferred.
        self.assertEqual("deferred", result["decision"])
        self.assertEqual(2, result["failure_cases_used"])

    def test_tenant_scoped_calls_use_a_scoped_engine(self):
        engine = EvolutionEngine(self.store, seed_defaults=True)
        result = engine.auto_propose("llm-review", "other-tenant")
        self.assertEqual("deferred", result["decision"])
        self.assertEqual("other-tenant", self.store.evaluation_calls[-1][6])

    def test_candidate_generator_with_no_change_defers(self):
        class Generator:
            def generate(self, cases, base):
                return {"candidate_prompt": base, "generator": "test"}

        self.store.failure_cases = [{"id": 1, "category": "missed_issue", "payload": {}}]
        engine = EvolutionEngine(
            self.store, seed_defaults=False, candidate_generator=Generator(),
        )
        result = engine.auto_propose("llm-review")
        self.assertEqual("deferred", result["decision"])
        self.assertIn("root-cause analysis produced no prompt change", result["reason"])

    def test_candidate_generator_records_structured_evidence(self):
        class Generator:
            def generate(self, cases, base):
                return {
                    "candidate_prompt": base + "\n\nExtra structured rule.",
                    "generator": "root-cause-v1", "failure_cases": 1,
                    "clusters": [{"category": "missed_issue"}],
                    "change_diff": "diff", "candidate": {"rule": "x"},
                    "generation": {"llm_calls": 1},
                }

        self.store.failure_cases = [{"id": 1, "category": "missed_issue", "payload": {}}]
        engine = EvolutionEngine(
            self.store, seed_defaults=False, candidate_generator=Generator(),
        )
        result = engine.auto_propose("llm-review")
        self.assertIn("candidate_change", result)
        self.assertEqual("root-cause-v1", result["candidate_change"]["generator"])
        self.assertEqual(1, result["failure_cases_used"])
        self.assertIn("validation_sha256", result["evaluation_data"])
        self.assertEqual(1, len(self.store.updates))
        metrics = self.store.updates[0][2]
        self.assertFalse(metrics["structured_candidate"]["source_code_changes_allowed"])
        self.assertEqual("root-cause-v1", metrics["structured_candidate"]["generator"])


class RegressionEvaluatorTests(unittest.TestCase):
    def test_scores_matches_and_false_positives(self):
        cases = [{"name": "case", "diff": DIFF, "expected": [
            {"path": "app.py", "line": 1, "min_severity": "high"},
        ]}]
        report = RegressionEvaluator(
            lambda _prompt: LocalRuleReviewer(),
        ).run(DEFAULT_PROMPT, cases)
        self.assertEqual(1.0, report["recall"])
        self.assertEqual(1.0, report["precision"])
        self.assertEqual(1, report["cases"])
        self.assertEqual([], report["errors"])

    def test_reports_evaluation_errors(self):
        class Broken:
            name = "broken"

            def review(self, diff, parsed):
                raise RuntimeError("boom")

        cases = [{"name": "case", "diff": DIFF, "expected": []}]
        report = RegressionEvaluator(lambda _prompt: Broken()).run("prompt", cases)
        self.assertEqual(1, len(report["errors"]))
        self.assertEqual(0.0, report["success_rate"])


if __name__ == "__main__":
    unittest.main()
