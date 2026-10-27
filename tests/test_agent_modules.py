"""Guard the split agent runtime boundaries."""
from pathlib import Path
import unittest

from evoagent.agentic_core import AgenticReviewer
from evoagent.reviewer import Reviewer


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
        for path in (PACKAGE / "agent").glob("*.py"):
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("ReviewerBackref", source, path.name)
            self.assertNotIn("self.reviewer", source, path.name)

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
