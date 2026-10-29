"""Guard the Agent Core package layout."""
from pathlib import Path
import unittest


AGENT_MODULES = {
    "__init__.py", "assignments.py", "bounded_role.py", "collaborators.py",
    "context_manager.py", "findings.py", "harness.py", "lead.py",
    "lead_session.py", "memory.py", "memory_hooks.py", "orchestration.py",
    "repository_tools.py", "review_rules.py", "reviewer.py", "reviewers.py",
    "runtime.py", "scanning.py", "session.py", "telemetry.py", "tools.py",
}


class AgentCorePackageTests(unittest.TestCase):
    def test_agent_package_contains_the_moved_modules(self):
        agent = Path(__file__).resolve().parents[1] / "evoagent" / "agent"
        self.assertEqual(
            AGENT_MODULES,
            {path.name for path in agent.glob("*.py")},
        )

    def test_base_reviewer_and_agentic_reviewer_are_distinct(self):
        from evoagent.agent.reviewer import AgenticReviewer
        from evoagent.agent.reviewers import Reviewer

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
