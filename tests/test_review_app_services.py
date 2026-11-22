"""Guard the review application-layer split."""
from pathlib import Path
import unittest

from evoagent.app.review_execution import mark_dead_letter
from evoagent.app.reviews import mark_dead_letter as exported


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "evoagent" / "app"


class ReviewAppServiceTests(unittest.TestCase):
    def test_reviews_facade_is_small_and_composes_two_services(self):
        source = (APP / "reviews.py").read_text(encoding="utf-8")
        # +lines: batch details/accept pass-through for the batched engine.
        self.assertLessEqual(len(source.splitlines()), 165)
        self.assertIn("ReviewExecutionService(", source)
        self.assertIn("ReviewLifecycleService(", source)

    def test_execution_and_lifecycle_stay_focused(self):
        execution = (APP / "review_execution.py").read_text(encoding="utf-8")
        lifecycle = (APP / "review_lifecycle.py").read_text(encoding="utf-8")
        # +lines: batched-engine dispatch, paused-delivery short-circuit and
        # the resume top-up (additional_tokens) validation.
        self.assertLessEqual(len(execution.splitlines()), 290)
        self.assertLessEqual(len(lifecycle.splitlines()), 265)
        self.assertIn("class ReviewExecutionService", execution)
        self.assertIn("class ReviewLifecycleService", lifecycle)

    def test_dead_letter_is_reexported_for_compatibility(self):
        self.assertIs(exported, mark_dead_letter)

    def test_application_modules_do_not_import_adapters(self):
        for path in APP.glob("review*.py"):
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("from ..adapters", source, path.name)
            self.assertNotIn("from ..agent", source, path.name)


if __name__ == "__main__":
    unittest.main()
