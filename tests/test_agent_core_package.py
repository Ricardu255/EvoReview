"""Guard the Agent Core migration and its compatibility wrappers."""
import importlib
from pathlib import Path
import unittest
import warnings


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


def load_wrapper(name):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return importlib.import_module(name)


class AgentCorePackageTests(unittest.TestCase):
    def test_old_modules_are_compatibility_wrappers(self):
        for old_name, new_name in WRAPPERS.items():
            wrapper = load_wrapper("evoagent." + old_name)
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

        Exported = load_wrapper("evoagent.reviewer").Reviewer
        self.assertIs(Reviewer, Exported)
        self.assertIsNot(Reviewer, AgenticReviewer)

    def test_agent_core_only_imports_the_llm_adapter(self):
        agent = Path(__file__).resolve().parents[1] / "evoagent" / "agent"
        for path in agent.glob("*.py"):
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("from ..app", source, path.name)
            # The concrete LLM client is the one intentional adapter dependency
            # of the reviewer implementations; everything else stays inside agent.
            normalized = source.replace("from ..adapters.llm", "")
            self.assertNotIn("from ..adapters", normalized, path.name)


if __name__ == "__main__":
    unittest.main()
