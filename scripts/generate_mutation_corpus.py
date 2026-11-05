"""Generate a deterministic mutation-based evaluation corpus.

The corpus is synthetic and is intended for mechanism/regression evaluation.
It is not production ground truth. Generated output is written outside the
package unless an explicit path is supplied.
"""
import argparse
import json
import os
import sys


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from evoagent.evaluation.mutation_corpus import (  # noqa: E402
    OPERATORS, generate_mutation_cases, write_jsonl,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate labelled synthetic review cases by mutation.",
    )
    parser.add_argument(
        "--output", default=os.path.join(
            ROOT, "output", "mutation-corpus", "pr_diff_mutation.jsonl",
        ),
    )
    parser.add_argument("--per-rule", type=int, default=4)
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args()

    cases = generate_mutation_cases(per_rule=args.per_rule, seed=args.seed)
    directory = os.path.dirname(os.path.abspath(args.output))
    os.makedirs(directory, exist_ok=True)
    count = write_jsonl(cases, args.output)
    print(json.dumps({
        "output": args.output,
        "cases": count,
        "operators": len(OPERATORS),
        "seed": args.seed,
        "kind": "mutation-synthetic",
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
