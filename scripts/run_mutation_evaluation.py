"""Replay the deterministic mutation corpus through the review harness.

Usage:
    python scripts/run_mutation_evaluation.py --per-rule 4 --seed 1
"""
import argparse
import json
import os
import sys


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from evoagent.agent.review_rules import ContextRuleReviewer  # noqa: E402
from evoagent.agent.reviewers import CompositeReviewer, LocalRuleReviewer  # noqa: E402
from evoagent.evaluation.harness import EndToEndEvaluationHarness  # noqa: E402
from evoagent.evaluation.mutation_corpus import generate_mutation_cases  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Replay synthetic mutation cases with deterministic reviewers.",
    )
    parser.add_argument("--per-rule", type=int, default=4)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    cases = generate_mutation_cases(per_rule=args.per_rule, seed=args.seed)
    reviewer = CompositeReviewer([LocalRuleReviewer(), ContextRuleReviewer()])
    report = EndToEndEvaluationHarness().run(
        reviewer, cases, name="mutation-synthetic",
    )
    metrics = report["metrics"]
    output = {
        "name": report["name"],
        "dataset": report["dataset"],
        "metrics": metrics,
        "by_split": report["by_split"],
    }
    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    if args.output:
        os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as handle:
            json.dump(output, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")


if __name__ == "__main__":
    main()
