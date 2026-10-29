"""Guard the pure domain package layout."""
from pathlib import Path
import unittest


DOMAIN_MODULES = {
    "__init__.py", "models.py", "modes.py", "gates.py", "diff_parser.py",
    "finding_identity.py", "report.py",
}


class DomainPackageTests(unittest.TestCase):
    def test_domain_contains_only_pure_modules(self):
        domain = Path(__file__).resolve().parents[1] / "evoagent" / "domain"
        self.assertEqual(
            DOMAIN_MODULES,
            {path.name for path in domain.glob("*.py")},
        )
        for path in domain.glob("*.py"):
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("from ..", source, path.name)
            self.assertNotIn("from ...", source, path.name)

    def test_review_rules_lives_in_agent_not_domain(self):
        from evoagent.agent.review_rules import ContextRuleReviewer

        self.assertTrue(ContextRuleReviewer)
        self.assertFalse(
            (
                Path(__file__).resolve().parents[1]
                / "evoagent" / "domain" / "review_rules.py"
            ).exists()
        )


if __name__ == "__main__":
    unittest.main()
