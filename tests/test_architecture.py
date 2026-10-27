"""Guard the dependency direction introduced by the structural split."""
import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "evoagent"


def imported_modules(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


class ArchitectureTests(unittest.TestCase):
    def test_service_does_not_construct_infrastructure(self):
        forbidden = {
            "postgres_store", "context_manager", "memory", "observability",
            "rollout", "verifier", "patching", "fixer", "skills",
            "skill_evolution", "auth", "evolution_v2", "llm",
        }
        imports = imported_modules(PACKAGE / "service.py")
        leaked = forbidden.intersection(imports)
        self.assertEqual(set(), leaked, "ReviewService must not wire %s" % sorted(leaked))

    def test_bootstrap_owns_concrete_construction(self):
        imports = imported_modules(PACKAGE / "bootstrap.py")
        expected = {
            "agentic_core", "auth", "context_manager", "evolution",
            "evolution_v2", "fixer", "github", "harness", "llm", "memory",
            "observability", "patching", "postgres_store", "reviewer",
            "rollout", "skill_evolution", "skills", "task_queue", "verifier",
        }
        self.assertEqual(set(), expected.difference(imports))

    def test_store_contract_is_only_a_compatibility_export(self):
        source = (PACKAGE / "store_contract.py").read_text(encoding="utf-8")
        self.assertIn("from .ports.stores import", source)
        self.assertNotIn("class StoreProtocol", source)

    def test_store_ports_do_not_import_adapters(self):
        for path in (PACKAGE / "ports" / "stores").glob("*.py"):
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("postgres_store", source, path.name)
            self.assertNotIn("from ..store import", source, path.name)

    def test_application_layer_does_not_import_infrastructure(self):
        forbidden = {
            "postgres_store", "task_queue", "github", "llm", "memory",
            "observability", "rollout", "verifier", "patching", "fixer",
        }
        for path in (PACKAGE / "app").glob("*.py"):
            imports = imported_modules(path)
            leaked = forbidden.intersection(imports)
            self.assertEqual(set(), leaked, "%s imports %s" % (path.name, sorted(leaked)))


if __name__ == "__main__":
    unittest.main()
