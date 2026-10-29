"""Guard the infrastructure adapter package and its wrappers."""
import importlib
from pathlib import Path
import unittest
import warnings


WRAPPERS = {
    "task_queue": "task_queue",
    "github": "github",
    "llm": "llm",
    "auth": "auth",
    "observability": "observability",
    "metrics": "metrics",
    "rollout": "rollout",
    "fixer": "fixer",
    "patching": "patching",
    "verifier": "verifier",
}


def load_wrapper(name):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return importlib.import_module(name)


class AdapterPackageTests(unittest.TestCase):
    def test_old_modules_are_compatibility_wrappers(self):
        for old_name, new_name in WRAPPERS.items():
            wrapper = load_wrapper("evoagent." + old_name)
            impl = importlib.import_module("evoagent.adapters." + new_name)
            self.assertTrue(wrapper.__all__, old_name)
            for name in wrapper.__all__:
                self.assertIs(getattr(wrapper, name), getattr(impl, name), name)
            source = Path(wrapper.__file__).read_text(encoding="utf-8")
            self.assertIn("as _impl", source, old_name)
            self.assertIn("__getattr__", source, old_name)

    def test_adapters_contains_the_moved_modules(self):
        adapters = Path(__file__).resolve().parents[1] / "evoagent" / "adapters"
        self.assertEqual(
            {name + ".py" for name in WRAPPERS.values()} | {"__init__.py"},
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
