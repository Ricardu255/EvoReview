"""Guard the Agent Core migration and its compatibility wrappers."""
import importlib
from pathlib import Path
import unittest


WRAPPERS = {
    "bounded_role": "bounded_role",
    "harness": "harness",
    "lead_session": "lead_session",
    "context_manager": "context_manager",
    "memory": "memory",
    "runtime": "runtime",
    "telemetry": "telemetry",
    "repository_tools": "repository_tools",
    "reviewer": "reviewers",
}


class AgentCorePackageTests(unittest.TestCase):
    def test_old_modules_are_compatibility_wrappers(self):
        for old_name, new_name in WRAPPERS.items():
            wrapper = importlib.import_module("evoagent." + old_name)
            impl = importlib.import_module("evoagent.agent." + new_name)
            self.assertTrue(wrapper.__all__, old_name)
            for name in wrapper.__all__:
                self.assertIs(getattr(wrapper, name), getattr(impl, name), name)
            source = Path(wrapper.__file__).read_text(encoding="utf-8")
            self.assertIn("as _impl", source, old_name)
            self.assertIn("__getattr__", source, old_name)

    def test_agent_package_contains_the_moved_modules(self):
        agent = Path(__file__).resolve().parents[1] / "evoagent" / "agent"
        self.assertTrue(
            {name + ".py" for name in WRAPPERS.values()}.issubset(
                {path.name for path in agent.glob("*.py")}
            )
        )

    def test_base_reviewer_and_agentic_reviewer_are_distinct(self):
        from evoagent.agent.reviewer import AgenticReviewer
        from evoagent.agent.reviewers import Reviewer
        from evoagent.reviewer import Reviewer as Exported

        self.assertIs(Reviewer, Exported)
        self.assertIsNot(Reviewer, AgenticReviewer)

    def test_agent_core_does_not_import_app_or_adapters(self):
        agent = Path(__file__).resolve().parents[1] / "evoagent" / "agent"
        for path in agent.glob("*.py"):
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("from ..app", source, path.name)
            self.assertNotIn("from ..adapters", source, path.name)


if __name__ == "__main__":
    unittest.main()
