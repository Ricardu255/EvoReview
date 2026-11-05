"""Run a real-model evaluation with visible per-case progress.

Example:
    python -u scripts/run_real_model_evaluation.py \
      --arm single-llm --limit 20 --effort low
"""
import argparse
import json
import os
import sys
import time


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from evoagent.adapters.llm import JsonChatClient  # noqa: E402
from evoagent.evaluation.arms import ProductArmReviewer, SingleModelReviewer  # noqa: E402
from evoagent.evaluation.harness import load_jsonl  # noqa: E402
from evoagent.evaluation.v2 import ProductionEvaluationHarness  # noqa: E402


def build_reviewer(arm, client, token_budget, time_budget):
    if arm in {"single-llm", "single-llm-scanner"}:
        return SingleModelReviewer(arm, client, token_budget, time_budget)
    if arm == "full-agentic":
        return ProductArmReviewer(
            arm, client, total_token_budget=token_budget,
            total_time_budget_seconds=time_budget,
        )
    raise ValueError("unsupported arm: %s" % arm)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a real-model replay with per-case progress.",
    )
    parser.add_argument(
        "--dataset", default=os.path.join(
            ROOT, "evaluation_data", "pr_diff_100.jsonl",
        ),
    )
    parser.add_argument("--arm", default="single-llm", choices=(
        "single-llm", "single-llm-scanner", "full-agentic",
    ))
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--base-url", default=os.getenv(
        "EVOAGENT_LLM_BASE_URL", "https://api.deepseek.com",
    ))
    parser.add_argument("--api-key", default=(
        os.getenv("EVOAGENT_DEEPSEEK_API_KEY") or
        os.getenv("EVOAGENT_LLM_API_KEY") or ""
    ))
    parser.add_argument("--model", default=os.getenv(
        "EVOAGENT_LLM_MODEL", "deepseek-flash",
    ))
    parser.add_argument("--provider", default=os.getenv(
        "EVOAGENT_LLM_PROVIDER", "deepseek",
    ))
    parser.add_argument("--effort", default="low")
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--token-budget", type=int, default=6000)
    parser.add_argument("--time-budget", type=int, default=90)
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    if not args.api_key:
        parser.error("set EVOAGENT_DEEPSEEK_API_KEY or pass --api-key")
    cases = load_jsonl(args.dataset)[args.offset:args.offset + args.limit]
    if not cases:
        parser.error("no cases selected")

    client = JsonChatClient(
        args.base_url, args.api_key, args.model, provider=args.provider,
        timeout=args.timeout,
        extra_payload=({"effort": args.effort} if args.effort else None),
    )
    reviewer = build_reviewer(
        args.arm, client, args.token_budget, args.time_budget,
    )
    harness = ProductionEvaluationHarness()
    totals = harness._empty_totals()
    results = []
    started = time.monotonic()
    print("START arm=%s model=%s cases=%d effort=%s" % (
        args.arm, args.model, len(cases), args.effort,
    ), flush=True)
    for index, case in enumerate(cases, 1):
        result = harness._run_case(reviewer, case)
        results.append(result)
        harness._accumulate(totals, result)
        cumulative = harness._metrics(totals)
        print(
            "PROGRESS %d/%d id=%s exec=%s tp=%d fp=%d fn=%d "
            "calls=%d tokens=%d latency_ms=%d cum_p=%.4f cum_r=%.4f cum_f1=%.4f"
            % (
                index, len(cases), case["id"], result.get("execution_success"),
                result.get("tp", 0), result.get("fp", 0), result.get("fn", 0),
                result.get("llm_calls", 0), result.get("total_tokens", 0),
                result.get("latency_ms", 0), cumulative["precision"],
                cumulative["recall"], cumulative["f1"],
            ),
            flush=True,
        )

    metrics = harness._metrics(totals)
    report = {
        "name": "real-%s-%d" % (args.arm, len(cases)),
        "arm": args.arm,
        "model": args.model,
        "effort": args.effort,
        "dataset": {
            "path": os.path.abspath(args.dataset),
            "cases": len(cases),
            "risk_cases": sum(bool(case["expected_findings"]) for case in cases),
            "clean_cases": sum(not case["expected_findings"] for case in cases),
        },
        "metrics": metrics,
        "case_results": results,
        "duration_seconds": round(time.monotonic() - started, 2),
    }
    output = args.output or os.path.join(
        ROOT, "output", "real-model-eval",
        "%s-%d.json" % (args.arm, len(cases)),
    )
    os.makedirs(os.path.dirname(os.path.abspath(output)), exist_ok=True)
    with open(output, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    print("SUMMARY %s" % json.dumps({
        key: metrics.get(key) for key in (
            "precision", "recall", "f1", "high_risk_recall", "clean_accuracy",
            "exact_line_accuracy", "evidence_accuracy", "failure_rate",
            "average_llm_calls_per_pr", "average_total_tokens_per_pr",
            "average_latency_ms_per_pr",
        )
    }, ensure_ascii=False), flush=True)
    print("OUTPUT %s" % output, flush=True)


if __name__ == "__main__":
    main()
