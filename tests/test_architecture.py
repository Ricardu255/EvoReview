"""Guard the dependency direction, imports and final top-level layout."""
import ast
import importlib
from pathlib import Path
import pkgutil
import unittest


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "evoagent"

REMOVED_MODULES = (
    "agentic_core", "auth", "bounded_role", "context_manager", "diff_parser",
    "evaluation_arms", "evaluation_benchmark", "evaluation_experiments",
    "evaluation_harness", "evaluation_v2", "evolution", "evolution_proof",
    "evolution_v2", "finding_identity", "fixer", "gates", "github", "harness",
    "lead_session", "llm", "memory", "metrics", "models", "modes",
    "observability", "patching", "report", "repository_tools", "review_rules",
    "reviewer", "rollout", "runtime", "skill_evolution", "skills",
    "store_contract", "task_queue", "telemetry", "verifier",
)

MOVED_STORE_MODULES = (
    "store", "postgres_store", "sql_store", "prompt_store", "webhook_store",
)
LEGACY_MODULES = tuple(
    "evoagent." + name for name in REMOVED_MODULES + MOVED_STORE_MODULES
)


def module_parts(path):
    """Dotted parts of a source file and whether it is a package __init__."""
    parts = list(path.relative_to(ROOT).with_suffix("").parts)
    is_package = parts[-1] == "__init__"
    if is_package:
        parts.pop()
    return parts, is_package


def resolve_from(path, node):
    """Absolute dotted module for an ImportFrom node, or None."""
    parts, is_package = module_parts(path)
    package = parts if is_package else parts[:-1]
    if node.level:
        keep = len(package) - (node.level - 1)
        if keep <= 0:
            return None
        base = list(package[:keep])
    else:
        base = []
    if node.module:
        base.extend(node.module.split("."))
    return ".".join(base) if base else None


def imported_modules(path):
    """Absolute module paths imported by a file."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            resolved = resolve_from(path, node)
            if resolved:
                modules.add(resolved)
    return modules


def imported_targets(path):
    """Absolute modules plus ``module.attribute`` targets imported by a file."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    targets = set(imported_modules(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        resolved = resolve_from(path, node)
        if not resolved:
            continue
        for alias in node.names:
            if alias.name != "*":
                targets.add(resolved + "." + alias.name)
    return targets


def touches(target, prefix):
    return target == prefix or target.startswith(prefix + ".")


class ArchitectureTests(unittest.TestCase):
    def test_service_does_not_construct_stores_or_queues(self):
        forbidden = {
            "evoagent.adapters.stores.postgres.store",
            "evoagent.adapters.stores.sqlite.store",
            "evoagent.adapters.task_queue",
        }
        imports = imported_modules(PACKAGE / "service.py")
        leaked = {
            name for name in imports
            for prefix in forbidden if touches(name, prefix)
        }
        self.assertEqual(set(), leaked, "ReviewService must not wire %s" % sorted(leaked))

    def test_bootstrap_owns_concrete_construction(self):
        imports = imported_modules(PACKAGE / "bootstrap.py")
        expected = {
            "evoagent.agent.reviewer", "evoagent.agent.context_manager",
            "evoagent.agent.harness", "evoagent.agent.memory",
            "evoagent.agent.reviewers",
            "evoagent.adapters.auth", "evoagent.adapters.github",
            "evoagent.adapters.llm", "evoagent.adapters.observability",
            "evoagent.adapters.patching", "evoagent.adapters.rollout",
            "evoagent.adapters.stores.postgres.store",
            "evoagent.adapters.task_queue", "evoagent.adapters.verifier",
            "evoagent.evaluation.evolution", "evoagent.evaluation.evolution_v2",
            "evoagent.evaluation.skill_evolution", "evoagent.evaluation.skills",
        }
        self.assertEqual(set(), expected.difference(imports))

    def test_store_ports_do_not_import_adapters(self):
        for path in (PACKAGE / "ports" / "stores").glob("*.py"):
            imports = imported_modules(path)
            leaked = {
                name for name in imports
                if touches(name, "evoagent.adapters")
            }
            self.assertEqual(set(), leaked, "%s imports %s" % (path.name, sorted(leaked)))

    def test_application_layer_does_not_import_infrastructure(self):
        for path in (PACKAGE / "app").glob("*.py"):
            imports = imported_modules(path)
            leaked = {
                name for name in imports
                if touches(name, "evoagent.adapters")
                or touches(name, "evoagent.agent")
            }
            self.assertEqual(set(), leaked, "%s imports %s" % (path.name, sorted(leaked)))


class RootLayoutTests(unittest.TestCase):
    def test_root_contains_only_stable_entry_modules(self):
        self.assertEqual(
            {
                "__init__", "__main__", "api", "bootstrap", "config", "errors",
                "prompts", "service", "time_utils",
            },
            {path.stem for path in PACKAGE.glob("*.py")},
        )

    def test_subpackages_exist(self):
        for name in ("agent", "app", "adapters", "domain", "evaluation", "ports"):
            self.assertTrue((PACKAGE / name / "__init__.py").is_file(), name)

    def test_removed_compatibility_modules_are_gone(self):
        self.assertEqual(38, len(REMOVED_MODULES))
        self.assertEqual(38, len(set(REMOVED_MODULES)))
        for name in REMOVED_MODULES:
            self.assertFalse((PACKAGE / name).exists(), name)

    def test_old_store_modules_are_not_toplevel(self):
        self.assertEqual(5, len(MOVED_STORE_MODULES))
        for name in MOVED_STORE_MODULES:
            self.assertFalse((PACKAGE / name).exists(), name)

    def test_all_package_modules_import(self):
        import evoagent

        failures = []
        for info in pkgutil.walk_packages(evoagent.__path__, evoagent.__name__ + "."):
            try:
                importlib.import_module(info.name)
            except Exception as exc:
                failures.append((info.name, type(exc).__name__, str(exc)))
        self.assertEqual([], failures)

    def test_no_stale_legacy_imports(self):
        leaks = []
        for folder in ("evoagent", "tests", "scripts"):
            for path in (ROOT / folder).rglob("*.py"):
                for target in imported_targets(path):
                    if any(touches(target, legacy) for legacy in LEGACY_MODULES):
                        leaks.append("%s imports %s" % (path.relative_to(ROOT), target))
        self.assertEqual([], leaks, "use the new package paths instead: %s" % leaks)


if __name__ == "__main__":
    unittest.main()
