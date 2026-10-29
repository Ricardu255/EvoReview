"""Guard the infrastructure adapter package layout."""
from pathlib import Path
import unittest


ADAPTER_MODULES = {
    "__init__.py", "auth.py", "fixer.py", "github.py", "llm.py", "metrics.py",
    "observability.py", "patching.py", "rollout.py", "task_queue.py",
    "verifier.py",
}


class AdapterPackageTests(unittest.TestCase):
    def test_adapters_contains_the_moved_modules(self):
        adapters = Path(__file__).resolve().parents[1] / "evoagent" / "adapters"
        self.assertEqual(
            ADAPTER_MODULES,
            {path.name for path in adapters.glob("*.py")},
        )

    def test_adapters_only_import_agent_telemetry(self):
        adapters = Path(__file__).resolve().parents[1] / "evoagent" / "adapters"
        for path in adapters.glob("*.py"):
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("from ..app", source, path.name)
            normalized = source.replace("from ..agent.telemetry", "")
            self.assertNotIn("from ..agent", normalized, path.name)


if __name__ == "__main__":
    unittest.main()
