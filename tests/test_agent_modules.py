"""Guard the split agent runtime boundaries."""
from pathlib import Path
import unittest

from evoagent.agent.reviewer import AgenticReviewer
from evoagent.agent.reviewers import Reviewer


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "evoagent"


class AgentModuleTests(unittest.TestCase):
    def test_agentic_core_is_only_a_compatibility_reexport(self):
        source = (PACKAGE / "agentic_core.py").read_text(encoding="utf-8")
        self.assertIn("from .agent.reviewer import", source)
        self.assertLessEqual(len(source.splitlines()), 30)

    def test_reviewer_facade_stays_small(self):
        source = (PACKAGE / "agent" / "reviewer.py").read_text(encoding="utf-8")
        self.assertLessEqual(len(source.splitlines()), 320)

    def test_reviewer_is_composed_from_collaborators(self):
        self.assertEqual((Reviewer,), AgenticReviewer.__bases__)
        source = (PACKAGE / "agent" / "reviewer.py").read_text(encoding="utf-8")
        for attr in (
            "runtime", "findings", "tools", "sessions", "memory", "scanner",
            "lead", "assignments", "orchestrator",
        ):
            self.assertIn("self.%s = " % attr, source)
        self.assertIn("ReviewRuntime(", source)
        self.assertNotIn("Mixin", source)

    def test_collaborators_depend_on_runtime_not_reviewer(self):
        collaborator_modules = (
            "assignments.py", "lead.py", "memory_hooks.py", "orchestration.py",
            "scanning.py", "session.py", "tools.py",
        )
        for name in collaborator_modules:
            source = (PACKAGE / "agent" / name).read_text(encoding="utf-8")
            self.assertNotIn("ReviewerBackref", source, name)
            self.assertNotIn("self.reviewer", source, name)

    def test_collaborators_share_one_runtime(self):
        reviewer = AgenticReviewer(None, None)
        collaborators = (
            reviewer.tools, reviewer.memory, reviewer.sessions,
            reviewer.scanner, reviewer.lead, reviewer.assignments,
            reviewer.orchestrator,
        )
        for collaborator in collaborators:
            self.assertIs(collaborator.runtime, reviewer.runtime)
            self.assertFalse(hasattr(collaborator, "reviewer"))
        self.assertIs(reviewer.lead.memory_hooks, reviewer.memory)
        self.assertIs(reviewer.assignments.memory_hooks, reviewer.memory)
        self.assertIs(reviewer.assignments.sessions, reviewer.sessions)
        self.assertIs(reviewer.orchestrator.scanner, reviewer.scanner)
        self.assertIs(reviewer._memory_scopes, reviewer.runtime.memory_scopes)

    def test_runtime_token_budget_clamps_structured_overrides(self):
        reviewer = AgenticReviewer(
            None, None,
            structured_config={"budget_parameters": {"lead": 1, "critic": 999999}},
        )
        self.assertEqual(256, reviewer._token_budget("lead"))
        self.assertEqual(32000, reviewer._token_budget("critic"))

    def test_runtime_backref_is_explicit(self):
        source = (PACKAGE / "agent" / "collaborators.py").read_text(encoding="utf-8")
        self.assertNotIn("__getattr__", source)
        reviewer = AgenticReviewer(None, None)
        self.assertIs(reviewer.lead.store, reviewer.runtime.store)
        self.assertIs(reviewer.lead.client, reviewer.runtime.client)
        self.assertIs(reviewer.lead.context_manager, reviewer.runtime.context_manager)
        self.assertIs(reviewer.lead.memory_manager, reviewer.runtime.memory_manager)
        self.assertIs(reviewer.lead.rules, reviewer.runtime.rules)
        self.assertEqual(
            reviewer.lead.default_token_budget,
            reviewer.runtime.default_token_budget,
        )
        self.assertEqual(
            reviewer.lead.default_time_budget,
            reviewer.runtime.default_time_budget,
        )
        self.assertEqual(reviewer.lead.prompt_overlay, reviewer.runtime.prompt_overlay)
        self.assertIs(reviewer.lead.memory_scopes, reviewer.runtime.memory_scopes)
        self.assertIs(
            reviewer.lead.memory_scope_lock, reviewer.runtime.memory_scope_lock,
        )
        self.assertEqual(
            reviewer.runtime.token_budget("lead"),
            reviewer.lead.token_budget("lead"),
        )
        with self.assertRaises(AttributeError):
            reviewer.lead.not_a_runtime_field

    def test_agent_modules_do_not_import_application_or_adapters(self):
        forbidden = {
            "service", "bootstrap", "postgres_store", "task_queue",
            "app", "api",
        }
        for path in (PACKAGE / "agent").glob("*.py"):
            source = path.read_text(encoding="utf-8")
            for module in forbidden:
                self.assertNotIn("from ..%s import" % module, source, path.name)
                self.assertNotIn("from ..%s." % module, source, path.name)


if __name__ == "__main__":
    unittest.main()
