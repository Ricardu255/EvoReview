"""Tests for Agent Skill package parsing and dataset/ablation validation."""
import os
import tempfile
import unittest

from evoagent.agent.reviewers import LocalRuleReviewer, Reviewer
from evoagent.evaluation.skills import (
    MAX_RESOURCE_BYTES, MAX_SKILL_MD_BYTES, AgentSkill, SkillRegistry,
    _safe_relative_path, _split_skill_markdown,
)
from evoagent.evaluation.v2 import (
    FairAblationSuite, paired_bootstrap_comparison, validate_real_dataset,
)


def markdown(name="demo-skill", description="A useful review skill.",
             body="# Instructions\n\nCheck added lines.\n", extra=""):
    return (
        "---\nname: %s\ndescription: %s\n%s---\n\n%s"
        % (name, description, extra, body)
    )


class SkillMarkdownTests(unittest.TestCase):
    def test_split_rejects_invalid_documents(self):
        cases = [
            ("x" * (MAX_SKILL_MD_BYTES + 1), "exceeds"),
            ("body without frontmatter", "must start with YAML frontmatter"),
            ("---\nname: x\n", "not terminated"),
            ("---\n: [\n---\nbody", "invalid YAML"),
            ("---\n- a\n---\nbody", "must be an object"),
            ("---\nname: x\n---\n   ", "must not be empty"),
        ]
        for content, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    _split_skill_markdown(content)

    def test_split_handles_bom_and_crlf(self):
        content = "﻿" + markdown().replace("\n", "\r\n")
        frontmatter, body = _split_skill_markdown(content)
        self.assertEqual("demo-skill", frontmatter["name"])
        self.assertIn("Check added lines", body)


class SafeResourcePathTests(unittest.TestCase):
    def test_rejects_unsafe_paths(self):
        for value in ("", "/", "SKILL.md", "../secret", "a/../b", "a//b", "./a"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "skill resource path"):
                    _safe_relative_path(value)

    def test_normalises_backslashes(self):
        self.assertEqual("refs/notes.md", _safe_relative_path("\\refs\\notes.md\\"))


class AgentSkillTests(unittest.TestCase):
    def test_from_markdown_normalises_tools_and_resources(self):
        skill = AgentSkill.from_markdown(
            markdown(extra="allowed-tools: read_file search_diff\n"),
            resources={"b.md": "b", "a.md": "a"},
        )
        self.assertEqual(("read_file", "search_diff"), skill.allowed_tools)
        self.assertEqual(("a.md", "b.md"), skill.resource_paths)
        self.assertEqual("a", skill.read_resource("a.md"))
        self.assertEqual(64, len(skill.content_sha256))

        as_list = AgentSkill.from_markdown(
            markdown(extra="allowed-tools:\n  - read_file\n  - read_file\n"),
        )
        self.assertEqual(("read_file",), as_list.allowed_tools)

    def test_from_markdown_validation_errors(self):
        cases = [
            (markdown(name="other"), {"expected_name": "demo-skill"},
             "must match its package directory"),
            (markdown(name="Bad_Name"), {}, "lowercase letters"),
            (markdown(description='""'), {}, "description must contain"),
            (markdown(description="d" * 1537), {}, "description must contain"),
            (markdown(extra="allowed-tools: 3\n"), {}, "allowed-tools"),
            (markdown(extra="allowed-tools:\n  - 3\n"), {}, "allowed-tools"),
            (markdown(), {"resources": {"../x": "x"}}, "escapes its package"),
            (markdown(), {"resources": {"a.md": 1}}, "UTF-8 text"),
            (markdown(), {"resources": {"a.md": "x" * (MAX_RESOURCE_BYTES + 1)}},
             "exceeds size limit"),
            (markdown(), {"resources": {"r%d.md" % i: "x" for i in range(101)}},
             "too many resources"),
        ]
        for content, kwargs, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    AgentSkill.from_markdown(content, **kwargs)

    def test_from_directory_loads_text_resources_and_skips_binary(self):
        with tempfile.TemporaryDirectory() as root:
            directory = os.path.join(root, "demo-skill")
            os.makedirs(os.path.join(directory, "refs"))
            with open(os.path.join(directory, "SKILL.md"), "w", encoding="utf-8") as handle:
                handle.write(markdown())
            with open(os.path.join(directory, "refs", "guide.md"), "w", encoding="utf-8") as handle:
                handle.write("guide")
            with open(os.path.join(directory, "binary.bin"), "wb") as handle:
                handle.write(b"\xff\xfe\x00")
            skill = AgentSkill.from_directory(directory)
            self.assertEqual("demo-skill", skill.name)
            self.assertEqual(("refs/guide.md",), skill.resource_paths)
            self.assertEqual("guide", skill.read_resource("refs/guide.md"))

    def test_from_directory_rejects_oversized_resources_and_name_mismatch(self):
        with tempfile.TemporaryDirectory() as root:
            directory = os.path.join(root, "demo-skill")
            os.makedirs(directory)
            with open(os.path.join(directory, "SKILL.md"), "w", encoding="utf-8") as handle:
                handle.write(markdown())
            path = os.path.join(directory, "big.md")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("x" * (MAX_RESOURCE_BYTES + 1))
            with self.assertRaisesRegex(ValueError, "exceeds size limit"):
                AgentSkill.from_directory(directory)

        with tempfile.TemporaryDirectory() as root:
            directory = os.path.join(root, "other-name")
            os.makedirs(directory)
            with open(os.path.join(directory, "SKILL.md"), "w", encoding="utf-8") as handle:
                handle.write(markdown())
            with self.assertRaisesRegex(ValueError, "must match its package directory"):
                AgentSkill.from_directory(directory)

    def test_from_directory_rejects_symlinked_skill_markdown(self):
        with tempfile.TemporaryDirectory() as root:
            directory = os.path.join(root, "demo-skill")
            os.makedirs(directory)
            target = os.path.join(root, "source.md")
            with open(target, "w", encoding="utf-8") as handle:
                handle.write(markdown())
            try:
                os.symlink(target, os.path.join(directory, "SKILL.md"))
            except (OSError, NotImplementedError):
                self.skipTest("symlinks are unavailable")
            with self.assertRaisesRegex(ValueError, "symlinks are not allowed"):
                AgentSkill.from_directory(directory)

    def test_from_artifact_validation_and_resources(self):
        for artifact, message in (
            ("not-a-dict", "must be an object"),
            ({"name": "demo-skill", "files": {}}, "files.SKILL.md"),
            ({"name": "demo-skill", "files": {"SKILL.md": 1}}, "files.SKILL.md"),
        ):
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    AgentSkill.from_artifact(artifact)

        skill = AgentSkill.from_artifact({
            "name": "demo-skill",
            "files": {"SKILL.md": markdown(), "refs/a.md": "a"},
        })
        self.assertEqual("evolved-db", skill.source)
        self.assertEqual(("refs/a.md",), skill.resource_paths)

    def test_read_resource_errors_and_filesystem_fallback(self):
        skill = AgentSkill.from_markdown(markdown(), resources={"a.md": "a"})
        with self.assertRaises(ValueError):
            skill.read_resource("")
        with self.assertRaises(FileNotFoundError):
            skill.read_resource("missing.md")

        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "a.txt")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("disk")
            disk_skill = AgentSkill(
                name="demo-skill", description="d", instructions="i",
                content="c", directory=root, resource_paths=("a.txt",),
            )
            self.assertEqual("disk", disk_skill.read_resource("a.txt"))

            oversized = AgentSkill(
                name="demo-skill", description="d", instructions="i",
                content="c", directory=root, resource_paths=("big.txt",),
            )
            with open(os.path.join(root, "big.txt"), "w", encoding="utf-8") as handle:
                handle.write("x" * (MAX_RESOURCE_BYTES + 1))
            with self.assertRaisesRegex(ValueError, "exceeds size limit"):
                oversized.read_resource("big.txt")

            escaping = AgentSkill(
                name="demo-skill", description="d", instructions="i",
                content="c", directory="", resource_paths=("a.txt",),
            )
            with self.assertRaises(PermissionError):
                escaping.read_resource("a.txt")

    def test_catalog_runtime_and_artifact_views(self):
        skill = AgentSkill.from_markdown(markdown(), resources={"a.md": "a"})
        self.assertEqual({"name": "demo-skill", "description": "A useful review skill."},
                         skill.catalog_entry())
        runtime = skill.runtime_entry()
        self.assertEqual(["a.md"], runtime["resources"])
        self.assertIn("Check added lines", runtime["instructions"])
        artifact = skill.to_artifact()
        self.assertEqual(2, artifact["schema_version"])
        self.assertEqual("a", artifact["files"]["a.md"])


class SkillRegistryTests(unittest.TestCase):
    def test_register_list_unregister_and_catalog(self):
        with tempfile.TemporaryDirectory() as root:
            registry = SkillRegistry(root)
            with self.assertRaisesRegex(ValueError, "invalid scanner name"):
                registry.register("bad name", LocalRuleReviewer())
            registry.register(
                "scanner", LocalRuleReviewer(), permissions=("read_file",),
            )
            self.assertEqual([LocalRuleReviewer.name], [item.name for item in registry.reviewers()])
            listing = registry.list()
            self.assertEqual("scanner", listing[0]["kind"])
            self.assertEqual(["read_file"], listing[0]["permissions"])
            registry.unregister("scanner")
            self.assertEqual([], registry.reviewers())
            self.assertEqual([], registry.list())

    def test_reload_discovers_valid_skill_directories(self):
        with tempfile.TemporaryDirectory() as root:
            valid = os.path.join(root, "demo-skill")
            os.makedirs(valid)
            with open(os.path.join(valid, "SKILL.md"), "w", encoding="utf-8") as handle:
                handle.write(markdown())
            os.makedirs(os.path.join(root, "no-skill-file"))
            with open(os.path.join(root, "plain.txt"), "w", encoding="utf-8") as handle:
                handle.write("x")

            registry = SkillRegistry(os.path.join(root, "new-dir"))
            self.assertEqual([], registry.reload())  # creates the directory
            self.assertTrue(os.path.isdir(os.path.join(root, "new-dir")))

            registry = SkillRegistry(root)
            listing = registry.reload()
            agent = [item for item in listing if item["kind"] == "agent-skill"]
            self.assertEqual(1, len(agent))
            self.assertEqual("demo-skill", agent[0]["name"])
            self.assertEqual(["demo-skill"], [item.name for item in registry.agent_skills()])
            self.assertEqual("demo-skill", registry.catalog()[0]["name"])
            self.assertEqual("demo-skill", registry.get_agent_skill("demo-skill").name)
            self.assertIsNone(registry.get_agent_skill("missing"))


class DatasetValidationTests(unittest.TestCase):
    def case(self, split="train", repository="org/repo", source="public-github-pr",
             findings=None):
        return {
            "id": "c", "repository": repository, "pull_request": 1, "split": split,
            "source": {"kind": source}, "diff": "--- a/a.py\n+++ b/a.py\n@@ -0,0 +1 @@\n+x\n",
            "expected_findings": findings if findings is not None else [{
                "path": "a.py", "start_line": 1, "end_line": 1,
                "severity": "high", "should_comment": True,
            }],
        }

    def test_rejects_invalid_splits_and_labels(self):
        with self.assertRaisesRegex(ValueError, "train, validation or holdout"):
            validate_real_dataset([self.case(split="prod")])
        with self.assertRaisesRegex(ValueError, "expected_findings"):
            validate_real_dataset([self.case(findings="nope")])
        with self.assertRaisesRegex(ValueError, "should_comment"):
            validate_real_dataset([self.case(findings=[{
                "path": "a.py", "start_line": 1, "severity": "high",
            }])])
        with self.assertRaisesRegex(ValueError, "severity, path"):
            validate_real_dataset([self.case(findings=[{
                "should_comment": True,
            }])])

    def test_reports_minimum_provenance_and_isolation_gates(self):
        ready_cases = [
            self.case("train", "org/train"),
            self.case("validation", "org/validation"),
            self.case("holdout", "org/holdout"),
        ]
        ready = validate_real_dataset(ready_cases, minimum_cases=3)
        self.assertTrue(ready["ready"])
        self.assertEqual(3, ready["cases"])
        self.assertEqual(64, len(ready["dataset_sha256"]))

        blocked = validate_real_dataset(
            ready_cases + [self.case("train", "org/validation")], minimum_cases=3,
        )
        self.assertFalse(blocked["gates"]["repository_isolation"])
        self.assertIn("train:validation", blocked["repository_overlap"])

        unknown = validate_real_dataset(
            [self.case(source="synthetic")], minimum_cases=300,
        )
        self.assertFalse(unknown["gates"]["real_provenance"])
        self.assertFalse(unknown["gates"]["minimum_300_cases"])


def case_result(case_id="a", split="validation", tp=1):
    return {
        "id": case_id, "split": split, "expected": 1, "tp": tp, "fp": 0, "fn": 0,
        "severity_hits": tp, "high_total": 1, "high_hits": tp,
        "clean_hit": 0, "execution_success": 1, "e2e_success": 0,
        "repair_attempted": 0, "repair_passed": 0,
    }


def report(case_results):
    return {
        "case_results": case_results,
        "metrics": {"f1": 1.0},
    }


class BootstrapAndAblationTests(unittest.TestCase):
    def test_paired_bootstrap_requires_identical_case_order(self):
        with self.assertRaisesRegex(ValueError, "identical ordered case ids"):
            paired_bootstrap_comparison(
                report([case_result("a")]), report([case_result("b")]),
            )

    def test_paired_bootstrap_empty_and_identical_cases(self):
        empty = paired_bootstrap_comparison(report([]), report([]), metrics=("f1",))
        self.assertEqual({"delta": 0.0, "ci95": [0.0, 0.0], "iterations": 0},
                         empty["f1"])

        same = paired_bootstrap_comparison(
            report([case_result()]), report([case_result()]),
            iterations=20, metrics=("f1",),
        )
        self.assertEqual(0.0, same["f1"]["delta"])
        self.assertEqual(200, same["f1"]["iterations"])  # clamped minimum
        self.assertEqual(0.0, same["f1"]["ci95"][0])

    def test_fair_ablation_rejects_missing_and_unknown_arms(self):
        with self.assertRaisesRegex(ValueError, "missing ablation arms"):
            FairAblationSuite({}, "model", 1000)
        arms = {
            name: (lambda model, tokens: None)
            for name in ("multi-llm-no-critic", "full-agentic")
        }
        arms["rogue"] = lambda model, tokens: None
        with self.assertRaisesRegex(ValueError, "unknown ablation arms"):
            FairAblationSuite(arms, "model", 1000)

    def test_role_totals_and_split_view(self):
        self.assertEqual(
            {"critic": 2, "lead": 1},
            FairAblationSuite._role_totals([
                {"model_roles": {"lead": 1, "critic": 1}},
                {"model_roles": {"critic": 1}},
            ]),
        )
        arm = {
            "by_split": {"validation": {"f1": 1.0}},
            "case_results": [
                {"id": "a", "split": "validation"},
                {"id": "b", "split": "holdout"},
            ],
        }
        view = FairAblationSuite._split_view(arm, "validation")
        self.assertEqual({"f1": 1.0}, view["metrics"])
        self.assertEqual([{"id": "a", "split": "validation"}], view["case_results"])

    def test_paired_delta_helper_handles_empty_identical_and_mismatched_inputs(self):
        suite = FairAblationSuite(
            {"multi-llm-no-critic": lambda m, t: None,
             "full-agentic": lambda m, t: None},
            "model", 1000, require_production_ready=False, bootstrap_iterations=200,
        )
        same = suite._paired_delta(
            report([case_result()]), report([case_result()]), "f1", 0,
        )
        self.assertEqual(0.0, same["delta"])
        self.assertEqual([0.0, 0.0], same["ci95"])
        self.assertEqual({
            "delta": 0.0, "ci95": [0.0, 0.0], "iterations": 0,
        }, suite._paired_delta(report([]), report([]), "f1", 0))
        with self.assertRaisesRegex(ValueError, "identical ordered case ids"):
            suite._paired_delta(
                report([case_result("a")]), report([case_result("b")]), "f1", 0,
            )

    def test_fair_ablation_runs_paired_arms_on_an_offline_dataset(self):
        class ArmReviewer(Reviewer):
            name = "arm-reviewer"

            def review(self, diff, parsed):
                return []

        factories = {
            name: (lambda model, tokens: ArmReviewer())
            for name in ("multi-llm-no-critic", "full-agentic")
        }
        suite = FairAblationSuite(
            factories, "model", 1000, require_production_ready=False,
            bootstrap_iterations=200,
        )
        cases = [{
            "id": "holdout-1", "repository": "org/holdout", "pull_request": 1,
            "split": "holdout", "source": {"kind": "public-github-pr"},
            "diff": "--- a/a.py\n+++ b/a.py\n@@ -0,0 +1 @@\n+x = 1\n",
            "expected_findings": [{
                "path": "a.py", "start_line": 1, "end_line": 1,
                "severity": "low", "should_comment": True,
            }],
        }]
        report = suite.run(cases)
        self.assertEqual(2, len(report["arms"]))
        self.assertEqual("model", report["arms"]["full-agentic"]["fairness"]["model"])
        self.assertIn("model_role_calls", report["arms"]["full-agentic"]["execution"])
        self.assertIn("critic_vs_no_critic", report["comparisons"])
        self.assertIn("critic_gate", report)
        self.assertEqual(
            ["single-llm", "single-llm-scanner", "full-agentic-evolved-skill"],
            report["omitted_arms"],
        )


if __name__ == "__main__":
    unittest.main()
