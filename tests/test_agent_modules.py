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
