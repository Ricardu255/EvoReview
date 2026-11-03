"""Unit tests for Agent Skill artifact validation and mutation helpers."""
import unittest

from evoagent.evaluation.skill_evolution import (
    ARTIFACT_SCHEMA_VERSION, SkillEvolutionEngine, _migrate_legacy_artifact,
    validate_artifact,
)


def artifact_with_learned(rule_id="SEC-WEAK-HASH"):
    return {
        "name": "evolved-review",
        "files": {"SKILL.md": "\n".join([
            "---", "name: evolved-review",
            'description: "Replay validated review guidance."', "---", "",
            "# Review guidance", "",
            "<!-- evoagent:learned:%s:start -->" % rule_id,
            "## %s" % rule_id, "",
            "Inspect added behavior equivalent to `hashlib.md5(value)`.",
            "<!-- evoagent:learned:%s:end -->" % rule_id, "",
        ])},
    }


class ValidateArtifactTests(unittest.TestCase):
    def test_accepts_raw_markdown_and_canonicalises_the_schema(self):
        markdown = (
            "---\nname: evolved-review\n"
            "description: \"Apply project-specific guidance during review.\"\n"
            "---\n\n# Guidance\n\nCheck added lines.\n"
        )
        normalized = validate_artifact(markdown, "evolved-review")
        self.assertEqual("evolved-review", normalized["name"])
        self.assertEqual(ARTIFACT_SCHEMA_VERSION, normalized["schema_version"])
        self.assertIn("SKILL.md", normalized["files"])

    def test_skill_md_and_supporting_files_are_promoted_to_files(self):
        normalized = validate_artifact({
            "name": "evolved-review",
            "skill_md": (
                "---\nname: evolved-review\n"
                "description: \"Apply project-specific guidance during review.\"\n---\n\n# G\n"
            ),
            "supporting_files": {"reference.md": "extra"},
        }, "evolved-review")
        self.assertEqual({"SKILL.md", "reference.md"}, set(normalized["files"]))

    def test_legacy_rule_artifacts_are_migrated(self):
        migrated = _migrate_legacy_artifact({
            "name": "evolved-review",
            "rules": [
                {"rule_id": "sec-weak-hash", "match": "`md5(value)`",
                 "severity": "medium"},
                {"rule_id": "EMPTY", "match": "  "},
            ],
        }, "evolved-review")
        content = migrated["files"]["SKILL.md"]
        self.assertIn("evoagent:learned:SEC-WEAK-HASH:start", content)
        self.assertIn("'md5(value)'", content)
        self.assertNotIn("evoagent:learned:EMPTY:start", content)

    def test_rejects_invalid_names_and_shapes(self):
        with self.assertRaisesRegex(ValueError, "invalid Agent Skill name"):
            validate_artifact("md", "Bad Name")
        with self.assertRaisesRegex(ValueError, "must be an object"):
            validate_artifact(42, "evolved-review")
        with self.assertRaisesRegex(ValueError, "must match skill_name"):
            validate_artifact(artifact_with_learned(), "other-skill")


class BuildCandidateTests(unittest.TestCase):
    def setUp(self):
        self.base = SkillEvolutionEngine.empty_artifact("evolved-review")

    def test_missed_issue_adds_a_learned_block(self):
        result = SkillEvolutionEngine.build_candidate_artifact(
            "evolved-review", self.base, [{
                "id": "f1", "category": "missed_issue",
                "payload": {"finding": {
                    "rule_id": "SEC-WEAK-HASH", "severity": "medium",
                    "evidence": "digest = hashlib.md5(value)",
                }},
            }],
        )
        self.assertEqual(["SEC-WEAK-HASH"], result["learned_rule_ids"])
        self.assertEqual(["f1"], result["used_feedback_ids"])
        self.assertIn(
            "evoagent:learned:SEC-WEAK-HASH:start",
            result["artifact"]["files"]["SKILL.md"],
        )

    def test_false_positive_removes_an_existing_learned_block(self):
        base = validate_artifact(artifact_with_learned(), "evolved-review")
        result = SkillEvolutionEngine.build_candidate_artifact(
            "evolved-review", base, [{
                "id": "f2", "category": "false_positive",
                "payload": {"finding": {"rule_id": "SEC-WEAK-HASH"}},
            }],
        )
        self.assertEqual(["SEC-WEAK-HASH"], result["removed_rule_ids"])
        self.assertEqual(["f2"], result["used_feedback_ids"])
        self.assertNotIn(
            "evoagent:learned:SEC-WEAK-HASH:start",
            result["artifact"]["files"]["SKILL.md"],
        )

    def test_skips_duplicate_invalid_and_unsafe_feedback(self):
        base = validate_artifact(artifact_with_learned(), "evolved-review")
        result = SkillEvolutionEngine.build_candidate_artifact(
            "evolved-review", base, [
                {"id": "dup", "category": "missed_issue", "payload": {"finding": {
                    "rule_id": "SEC-WEAK-HASH", "evidence": "x = 1",
                }}},
                {"id": "bad-rule", "category": "missed_issue", "payload": {"finding": {
                    "rule_id": "123-BAD", "evidence": "x = 1",
                }}},
                {"id": "bad-cat", "category": "other", "payload": {"finding": {
                    "rule_id": "SEC-OTHER", "evidence": "x = 1",
                }}},
                {"id": "no-evidence", "category": "missed_issue", "payload": {
                    "finding": {"rule_id": "SEC-NO-EVIDENCE"},
                }},
                {"id": "newline", "category": "missed_issue", "payload": {"finding": {
                    "rule_id": "SEC-NEWLINE", "evidence": "a\nb",
                }}},
                {"id": "too-long", "category": "missed_issue", "payload": {"finding": {
                    "rule_id": "SEC-LONG", "evidence": "x" * 241,
                }}},
                {"id": "no-payload", "category": "missed_issue"},
            ],
        )
        self.assertEqual([], result["learned_rule_ids"])
        self.assertEqual([], result["removed_rule_ids"])
        self.assertEqual([], result["used_feedback_ids"])

    def test_severity_is_preserved_in_the_learned_block(self):
        result = SkillEvolutionEngine.build_candidate_artifact(
            "evolved-review", self.base, [{
                "id": "f", "category": "missed_issue", "payload": {"finding": {
                    "rule_id": "SEC-CRITICAL", "severity": "critical",
                    "evidence": "eval(x)",
                }},
            }],
        )
        self.assertIn("at `critical` severity", result["artifact"]["files"]["SKILL.md"])


class _Store:
    def __init__(self, validation=3, holdout=2):
        self.validation = validation
        self.holdout = holdout
        self.failures = []
        self.payloads = {}
        self.active = None
        self.saved_versions = []
        self.runs = []
        self.resolved = []
        self.activated = []

    def list_evaluation_cases(self, split, include, limit, tenant_id=None):
        count = self.validation if split == "validation" else self.holdout
        return [{"id": "%s-%d" % (split, index)} for index in range(count)]

    def get_active_skill_artifact(self, skill_name, tenant_id):
        return self.active

    def list_failure_cases(self, unresolved_only, limit, tenant_id=None):
        return list(self.failures)

    def get_task_payload(self, task_id):
        return self.payloads.get(task_id)

    def save_skill_artifact(self, skill_name, artifact, score, active, tenant_id):
        version = {
            "tenant_id": tenant_id, "skill_name": skill_name,
            "version": len(self.saved_versions) + 1, "score": score,
            "active": active, "parent_version": None,
            "artifact_sha256": "sha-%d" % (len(self.saved_versions) + 1),
            "created_at": "2026-01-01T00:00:00Z",
        }
        self.saved_versions.append((artifact, active))
        return version

    def save_skill_evolution_run(self, run):
        self.runs.append(run)

    def resolve_failure_cases(self, ids):
        self.resolved.extend(ids)

    def activate_skill_artifact(self, skill_name, version, tenant_id):
        self.activated.append((skill_name, version, tenant_id))
        return version == 1


class SkillEvolutionHelperTests(unittest.TestCase):
    def setUp(self):
        self.store = _Store()
        self.engine = SkillEvolutionEngine(self.store, reviewer_factory=None)

    def test_status_reports_dataset_and_model_readiness(self):
        status = self.engine.status("evolved-review", "tenant-a")
        self.assertEqual(3, status["validation_cases"])
        self.assertEqual(2, status["holdout_cases"])
        self.assertFalse(status["model_configured"])
        self.assertFalse(status["ready"])
        self.assertIsNone(status["active_version"])

        active = {"version": 4, "artifact_sha256": "abc"}
        self.store.active = active
        engine = SkillEvolutionEngine(self.store, reviewer_factory=lambda _: None)
        status = engine.status("evolved-review", "tenant-a")
        self.assertEqual(4, status["active_version"])
        self.assertTrue(status["ready"])

    def test_metrics_non_regressing_checks_conditional_metrics(self):
        base = {
            "score": 0.5, "precision": 0.5, "recall": 0.5,
            "high_severity_recall": 0.5, "success_rate": 1.0,
            "positive_cases": 0, "clean_cases": 0,
            "severity_accuracy": 0.5, "clean_accuracy": 0.5,
        }
        # Conditional metrics are ignored when the baseline has no such cases.
        self.assertTrue(self.engine.metrics_non_regressing(
            {**base, "severity_accuracy": 0.4, "clean_accuracy": 0.4}, base,
        ))
        with_cases = {**base, "positive_cases": 1, "clean_cases": 1}
        self.assertFalse(self.engine.metrics_non_regressing(
            {**with_cases, "severity_accuracy": 0.4}, with_cases,
        ))
        self.assertFalse(self.engine.metrics_non_regressing(
            {**with_cases, "recall": 0.49}, with_cases,
        ))

    def test_auto_propose_defers_when_no_supported_signal_exists(self):
        self.store.failures = [{
            "id": 1, "category": "missed_issue", "task_id": "t1",
            "payload": {"finding": {
                "rule_id": "SEC-X", "evidence": "has\nnewline",
            }},
        }]
        result = self.engine.auto_propose("evolved-review")
        self.assertEqual("deferred", result["decision"])
        self.assertIsNone(result["version"])
        self.assertEqual([], result["learned_rule_ids"])

    def test_auto_propose_learns_evidence_from_the_task_payload(self):
        self.store.failures = [{
            "id": 1, "category": "missed_issue", "task_id": "t1",
            "payload": {"finding": {
                "rule_id": "SEC-WEAK-HASH", "severity": "medium",
                "path": "app.py", "line": 1,
            }},
        }]
        self.store.payloads["t1"] = (
            "--- a/app.py\n+++ b/app.py\n@@ -0,0 +1 @@\n"
            "+digest = hashlib.md5(value)\n"
        )
        result = self.engine.auto_propose("evolved-review")
        self.assertEqual(["SEC-WEAK-HASH"], result["learned_rule_ids"])
        self.assertEqual(1, result["failure_cases_used"])
        self.assertEqual("deferred", result["decision"])  # no reviewer configured
        self.assertEqual(1, len(self.store.saved_versions))

    def test_auto_propose_removes_false_positive_guidance(self):
        self.store.active = {
            "version": 1, "artifact": artifact_with_learned(),
            "artifact_sha256": "sha-1",
        }
        self.store.failures = [{
            "id": 2, "category": "false_positive", "task_id": "t2",
            "payload": {"finding": {"rule_id": "SEC-WEAK-HASH"}},
        }]
        result = self.engine.auto_propose("evolved-review")
        self.assertEqual(["SEC-WEAK-HASH"], result["removed_rule_ids"])
        self.assertEqual([], result["learned_rule_ids"])

    def test_auto_propose_rejects_invalid_skill_names(self):
        with self.assertRaisesRegex(ValueError, "invalid Agent Skill name"):
            self.engine.auto_propose("Bad Name")

    def test_evidence_from_task_handles_missing_and_malformed_payloads(self):
        self.assertEqual("", self.engine._evidence_from_task("missing", {}))
        self.store.payloads["t"] = "--- a/a.py\n+++ b/a.py\n@@ -0,0 +1 @@\n+danger()\n"
        self.assertEqual(
            "danger()",
            self.engine._evidence_from_task("t", {"path": "a.py", "line": 1}),
        )
        self.assertEqual(
            "", self.engine._evidence_from_task("t", {"path": "a.py", "line": "x"}),
        )
        self.assertEqual(
            "", self.engine._evidence_from_task("t", {"path": "b.py", "line": 1}),
        )

    def test_rollback_delegates_to_the_store(self):
        self.assertTrue(self.engine.rollback("evolved-review", 1, "tenant-a"))
        self.assertFalse(self.engine.rollback("evolved-review", 2, "tenant-a"))
        self.assertEqual(
            [("evolved-review", 1, "tenant-a"), ("evolved-review", 2, "tenant-a")],
            self.store.activated,
        )

    def test_skill_diff_and_public_version(self):
        before = {"files": {"SKILL.md": "a", "old.md": "x"}}
        after = {"files": {"SKILL.md": "b", "new.md": "y"}}
        diff = self.engine._skill_diff(before, after)
        self.assertEqual(["SKILL.md", "new.md", "old.md"], diff["changed_files"])
        self.assertIn("baseline_skill_md_sha256", diff)

        public = self.engine._public_version({
            "tenant_id": "t", "skill_name": "s", "version": 2,
            "score": 0.5, "active": True, "parent_version": None,
            "artifact_sha256": "h", "created_at": "now", "internal": "drop",
        })
        self.assertNotIn("internal", public)
        self.assertEqual(2, public["version"])


if __name__ == "__main__":
    unittest.main()
