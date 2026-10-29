"""Guard the dependency direction introduced by the structural split."""
import ast
import importlib
from pathlib import Path
import unittest
import warnings


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
    def test_service_does_not_construct_stores_or_queues(self):
        forbidden = {"postgres_store", "task_queue", "adapters.postgres_store",
                     "adapters.task_queue"}
        imports = imported_modules(PACKAGE / "service.py")
        leaked = forbidden.intersection(imports)
        self.assertEqual(set(), leaked, "ReviewService must not wire %s" % sorted(leaked))

    def test_bootstrap_owns_concrete_construction(self):
        imports = imported_modules(PACKAGE / "bootstrap.py")
        expected = {
            "agent.reviewer", "agent.context_manager", "agent.harness",
            "agent.memory", "agent.reviewers",
            "adapters.auth", "adapters.github", "adapters.llm",
            "adapters.observability", "adapters.patching", "adapters.rollout",
            "adapters.task_queue", "adapters.verifier",
            "evaluation.evolution", "evaluation.evolution_v2",
            "evaluation.skill_evolution", "evaluation.skills",
            "postgres_store",
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
        forbidden = {"adapters", "agent"}
        for path in (PACKAGE / "app").glob("*.py"):
            imports = imported_modules(path)
            leaked = {
                name for name in imports
                if name == "adapters" or name.startswith("adapters.")
                or name == "agent" or name.startswith("agent.")
            }
            self.assertEqual(set(), leaked, "%s imports %s" % (path.name, sorted(leaked)))


WRAPPER_MODULES = (
    "agentic_core", "auth", "bounded_role", "context_manager", "diff_parser",
    "evaluation_arms", "evaluation_benchmark", "evaluation_experiments",
    "evaluation_harness", "evaluation_v2", "evolution", "evolution_proof",
    "evolution_v2", "finding_identity", "fixer", "gates", "github", "harness",
    "lead_session", "llm", "memory", "metrics", "models", "modes",
    "observability", "patching", "report", "repository_tools", "review_rules",
    "reviewer", "rollout", "runtime", "skill_evolution", "skills",
    "store_contract", "task_queue", "telemetry", "verifier",
)


def module_parts(path):
    """Dotted parts of a source file, without the file suffix."""
    parts = list(path.relative_to(ROOT).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return parts


def imported_targets(path):
    """Every dotted module path an import touches, relative levels resolved."""
    parts = module_parts(path)
    package = parts if path.name == "__init__.py" else parts[:-1]
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError) as exc:
        raise AssertionError(
            "cannot parse %s: %s" % (path.relative_to(ROOT), exc)
        ) from exc
    targets = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets.extend(alias.name for alias in node.names)
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level:
            keep = len(package) - (node.level - 1)
            if keep <= 0:
                continue
            base = list(package[:keep])
        else:
            base = []
        if node.module:
            base.extend(node.module.split("."))
        if not base:
            continue
        targets.append(".".join(base))
        targets.extend(
            ".".join(base + [alias.name])
            for alias in node.names if alias.name != "*"
        )
    return targets


def wrapper_leaks(paths):
    """Imports that still route through a top-level compatibility wrapper."""
    prefixes = ["evoagent." + name for name in WRAPPER_MODULES]
    leaks = []
    seen = set()
    for path in paths:
        for target in imported_targets(path):
            for prefix in prefixes:
                if target == prefix or target.startswith(prefix + "."):
                    key = (str(path.relative_to(ROOT)), target)
                    if key not in seen:
                        seen.add(key)
                        leaks.append("%s imports %s" % key)
                    break
    return leaks


NON_WRAPPER_MODULES = {
    "__main__", "api", "bootstrap", "config", "errors", "postgres_store",
    "prompt_store", "prompts", "service", "sql_store", "store", "time_utils",
    "webhook_store",
}

AGENTIC_CORE_EXPORTS = {
    "AgenticReviewer": "evoagent.agent.reviewer",
    "MAX_TRACKED_SUMMARIES": "evoagent.agent.reviewer",
    "SUMMARY_CHECKPOINT": "evoagent.agent.reviewer",
    "BoundedRole": "evoagent.agent.bounded_role",
}


class CompatibilityWrapperTests(unittest.TestCase):
    def test_wrapper_registry_matches_the_package(self):
        on_disk = {
            path.stem for path in PACKAGE.glob("*.py")
        } - {"__init__"} - NON_WRAPPER_MODULES
        self.assertEqual(set(WRAPPER_MODULES), on_disk)
        self.assertEqual(38, len(WRAPPER_MODULES))
        self.assertEqual(38, len(set(WRAPPER_MODULES)))

    def test_package_imports_bypass_compatibility_wrappers(self):
        leaks = wrapper_leaks(PACKAGE.rglob("*.py"))
        self.assertEqual([], leaks, "import the new package path instead: %s" % leaks)

    def test_scripts_import_bypass_compatibility_wrappers(self):
        scripts = list((ROOT / "scripts").rglob("*.py"))
        self.assertGreaterEqual(len(scripts), 7)
        leaks = wrapper_leaks(scripts)
        self.assertEqual([], leaks, "import the new package path instead: %s" % leaks)

    def test_tests_import_bypass_compatibility_wrappers(self):
        leaks = wrapper_leaks((ROOT / "tests").rglob("*.py"))
        self.assertEqual([], leaks, "import the new package path instead: %s" % leaks)

    def test_every_wrapper_warns_on_import(self):
        for name in WRAPPER_MODULES:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                module = importlib.import_module("evoagent." + name)
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                importlib.reload(module)
            deprecations = [
                item for item in caught
                if issubclass(item.category, DeprecationWarning)
            ]
            self.assertEqual(1, len(deprecations), name)

    def test_explicit_reexports_keep_their_public_names(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            store_contract = importlib.import_module("evoagent.store_contract")
            agentic_core = importlib.import_module("evoagent.agentic_core")
        ports = importlib.import_module("evoagent.ports.stores")
        self.assertEqual(set(ports.__all__), set(store_contract.__all__))
        for name in store_contract.__all__:
            self.assertIs(getattr(store_contract, name), getattr(ports, name), name)
        self.assertEqual(set(AGENTIC_CORE_EXPORTS), set(agentic_core.__all__))
        for name, module_name in AGENTIC_CORE_EXPORTS.items():
            impl = importlib.import_module(module_name)
            self.assertIs(getattr(agentic_core, name), getattr(impl, name), name)


if __name__ == "__main__":
    unittest.main()
