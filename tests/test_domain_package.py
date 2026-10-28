"""Guard the pure domain package and its compatibility wrappers."""
import importlib
from pathlib import Path
import unittest


WRAPPERS = {
    "models": "models",
    "modes": "modes",
    "gates": "gates",
    "diff_parser": "diff_parser",
    "finding_identity": "finding_identity",
    "report": "report",
}


class DomainPackageTests(unittest.TestCase):
    def test_old_modules_are_compatibility_wrappers(self):
        for old_name, new_name in WRAPPERS.items():
            wrapper = importlib.import_module("evoagent." + old_name)
            impl = importlib.import_module("evoagent.domain." + new_name)
            self.assertTrue(wrapper.__all__, old_name)
            for name in wrapper.__all__:
                self.assertIs(getattr(wrapper, name), getattr(impl, name), name)
            source = Path(wrapper.__file__).read_text(encoding="utf-8")
            self.assertIn("as _impl", source, old_name)
            self.assertIn("__getattr__", source, old_name)

    def test_domain_contains_only_pure_modules(self):
        domain = Path(__file__).resolve().parents[1] / "evoagent" / "domain"
        self.assertEqual(
            {name + ".py" for name in WRAPPERS.values()} | {"__init__.py"},
            {path.name for path in domain.glob("*.py")},
        )
        for path in domain.glob("*.py"):
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("from ..", source, path.name)
            self.assertNotIn("from ...", source, path.name)

    def test_review_rules_lives_in_agent_not_domain(self):
        from evoagent.agent.review_rules import ContextRuleReviewer
        from evoagent.review_rules import ContextRuleReviewer as Exported

        self.assertIs(ContextRuleReviewer, Exported)
        self.assertFalse((Path(__file__).resolve().parents[1] / "evoagent" / "domain" / "review_rules.py").exists())


if __name__ == "__main__":
    unittest.main()
