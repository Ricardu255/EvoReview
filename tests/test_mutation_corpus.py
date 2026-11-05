"""Self-checks for the mutation-based synthetic corpus generator."""
import os
import tempfile
import unittest

from evoagent.agent.review_rules import ContextRuleReviewer
from evoagent.agent.reviewers import CompositeReviewer, LocalRuleReviewer
from evoagent.domain.diff_parser import parse_unified_diff
from evoagent.evaluation.harness import load_jsonl, validate_case
from evoagent.evaluation.mutation_corpus import (
    GENERATOR, OPERATORS, generate_mutation_cases, write_jsonl,
)


class MutationCorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = generate_mutation_cases(per_rule=2, seed=7)

    def test_generation_is_deterministic(self):
        self.assertEqual(
            self.cases, generate_mutation_cases(per_rule=2, seed=7),
        )
        self.assertTrue(all(
            case["source"]["generator"] == GENERATOR for case in self.cases
        ))

    def test_covers_every_supported_rule_and_clean_hard_negatives(self):
        expected_rules = {item["rule_id"] for item in OPERATORS}
        detected = {
            finding["rule_id"]
            for case in self.cases for finding in case["expected_findings"]
        }
        self.assertEqual(expected_rules, detected)
        self.assertTrue(any(
            not case["expected_findings"] and
            case["source"]["operator"] == "HARD-NEGATIVE"
            for case in self.cases
        ))

    def test_every_case_passes_the_shared_dataset_validator(self):
        for case in self.cases:
            with self.subTest(case=case["id"]):
                validate_case(case)

    def test_expected_findings_land_on_added_lines(self):
        for case in self.cases:
            parsed = parse_unified_diff(case["diff"])
            added = {(item.path, item.line) for item in parsed.added_lines}
            for finding in case["expected_findings"]:
                self.assertIn(
                    (finding["path"], finding["start_line"]), added,
                )

    def test_before_and_after_files_compile(self):
        for case in self.cases:
            for content in (case.get("before_files") or {}).values():
                compile(content, case["id"], "exec")
            for content in (case.get("after_files") or {}).values():
                compile(content, case["id"], "exec")

    def test_repository_split_is_disjoint_and_complete(self):
        repositories = {case["repository"] for case in self.cases}
        self.assertEqual(10, len(repositories))
        validation = {
            case["repository"] for case in self.cases
            if case["split"] == "validation"
        }
        holdout = {
            case["repository"] for case in self.cases
            if case["split"] == "holdout"
        }
        self.assertEqual(8, len(validation))
        self.assertEqual(2, len(holdout))
        self.assertFalse(validation & holdout)

    def test_rule_reviewer_detects_every_labelled_rule(self):
        reviewer = CompositeReviewer([
            LocalRuleReviewer(), ContextRuleReviewer(),
        ])
        for case in self.cases:
            if not case["expected_findings"]:
                continue
            with self.subTest(case=case["id"]):
                rule_id = case["expected_findings"][0]["rule_id"]
                detected = {
                    item.rule_id for item in reviewer.review(
                        case["diff"], parse_unified_diff(case["diff"]),
                    )
                }
                self.assertIn(rule_id, detected)

    def test_safe_pairs_do_not_trigger_the_labelled_rule(self):
        reviewer = CompositeRuleReviewer()
        for case in self.cases:
            if case["expected_findings"]:
                continue
            with self.subTest(case=case["id"]):
                findings = reviewer.review(
                    case["diff"], parse_unified_diff(case["diff"]),
                )
                self.assertEqual([], findings)

    def test_jsonl_round_trip(self):
        handle, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(handle)
        try:
            self.assertEqual(len(self.cases), write_jsonl(self.cases, path))
            loaded = load_jsonl(path)
            self.assertEqual(len(self.cases), len(loaded))
            self.assertEqual(self.cases[0]["id"], loaded[0]["id"])
        finally:
            os.unlink(path)


class CompositeRuleReviewer:
    def __init__(self):
        self.reviewer = CompositeReviewer([
            LocalRuleReviewer(), ContextRuleReviewer(),
        ])

    def review(self, diff, parsed):
        return self.reviewer.review(diff, parsed)


if __name__ == "__main__":
    unittest.main()
