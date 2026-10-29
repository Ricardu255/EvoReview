"""Guard the evaluation package layout."""
import importlib
from pathlib import Path
import unittest


EVALUATION_MODULES = {
    "__init__.py", "arms.py", "benchmark.py", "experiments.py", "harness.py",
    "v2.py", "evolution.py", "evolution_v2.py", "evolution_proof.py",
    "skill_evolution.py", "skills.py",
}


class EvaluationPackageTests(unittest.TestCase):
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
            EVALUATION_MODULES,
            {path.name for path in evaluation.glob("*.py")},
        )


if __name__ == "__main__":
    unittest.main()
