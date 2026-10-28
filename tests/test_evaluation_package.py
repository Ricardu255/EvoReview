"""Guard the evaluation package move and its compatibility wrappers."""
import importlib
from pathlib import Path
import unittest


WRAPPERS = {
    "evaluation_arms": "arms",
    "evaluation_benchmark": "benchmark",
    "evaluation_experiments": "experiments",
    "evaluation_harness": "harness",
    "evaluation_v2": "v2",
    "evolution": "evolution",
    "evolution_v2": "evolution_v2",
    "evolution_proof": "evolution_proof",
    "skill_evolution": "skill_evolution",
    "skills": "skills",
}


class EvaluationPackageTests(unittest.TestCase):
    def test_old_modules_are_compatibility_wrappers(self):
        for old_name, new_name in WRAPPERS.items():
            wrapper = importlib.import_module("evoagent." + old_name)
            impl = importlib.import_module("evoagent.evaluation." + new_name)
            self.assertTrue(wrapper.__all__, old_name)
            for name in wrapper.__all__:
                self.assertIs(getattr(wrapper, name), getattr(impl, name), name)
            source = Path(wrapper.__file__).read_text(encoding="utf-8")
            self.assertIn("as _impl", source, old_name)
            self.assertIn("__getattr__", source, old_name)

    def test_evaluation_dataset_paths_point_at_project_root(self):
        experiments = importlib.import_module("evoagent.evaluation.experiments")
        evolution_proof = importlib.import_module(
            "evoagent.evaluation.evolution_proof"
        )
        self.assertEqual(
            ("evaluation_data", "pr_diff_100.jsonl"),
            Path(experiments.DEFAULT_CONTROLLED_DATASET).parts[-2:],
        )
        self.assertEqual(
            ("evaluation_data", "prompt_evolution_130.jsonl"),
            Path(evolution_proof.DEFAULT_PROMPT_DATASET).parts[-2:],
        )

    def test_new_package_contains_all_moved_modules(self):
        evaluation = Path(__file__).resolve().parents[1] / "evoagent" / "evaluation"
        self.assertEqual(
            {name + ".py" for name in WRAPPERS.values()} | {"__init__.py"},
            {path.name for path in evaluation.glob("*.py")},
        )


if __name__ == "__main__":
    unittest.main()
