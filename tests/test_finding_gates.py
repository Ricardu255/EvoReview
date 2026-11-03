"""Tests for deterministic finding/release gates."""
import unittest

from evoagent.domain.diff_parser import parse_unified_diff
from evoagent.domain.gates import FindingGate
from evoagent.domain.models import Finding, Severity


DIFF = (
    "--- a/a.py\n"
    "+++ b/a.py\n"
    "@@ -1,2 +1,3 @@\n"
    " import os\n"
    "+value = eval(raw)\n"
    " return value\n"
)


def finding(**overrides):
    values = {
        "rule_id": "SEC-EVAL", "severity": Severity.MEDIUM,
        "title": "eval is unsafe", "explanation": "User input reaches eval.",
        "path": "a.py", "line": 2, "evidence": "value = eval(raw)",
        "fix": "Use json.loads.", "test": "Add a malformed-input test.",
        "confidence": 0.8,
    }
    values.update(overrides)
    return Finding(**values)


class FindingGateTests(unittest.TestCase):
    def setUp(self):
        self.parsed = parse_unified_diff(DIFF)
        self.gate = FindingGate()

    def test_accepts_a_medium_finding_with_exact_line_evidence(self):
        item = finding()
        result = self.gate.apply([item], self.parsed)
        self.assertEqual([item], result.accepted)
        self.assertEqual([], result.rejected)
        self.assertEqual(1, result.checks["accepted"])
        self.assertEqual(0, result.checks["rejected"])
        self.assertTrue(item.gate["passed"])
        self.assertTrue(item.gate["exact_location_evidence"])
        self.assertEqual(0, item.gate["strong_evidence_count"])

    def test_format_gate_rejects_invalid_locations_and_missing_fields(self):
        for overrides in (
            {"path": "other.py"},
            {"line": 1},
            {"rule_id": "  "},
            {"title": ""},
            {"explanation": ""},
        ):
            with self.subTest(overrides=overrides):
                item = finding(**overrides)
                result = self.gate.apply([item], self.parsed)
                self.assertEqual([], result.accepted)
                self.assertIn("format gate", result.rejected[0]["reasons"][0])
                self.assertEqual(1, result.checks["format"]["rejected"])

    def test_evidence_gate_rejects_unquoted_code_without_references(self):
        item = finding(evidence="unrelated text")
        result = self.gate.apply([item], self.parsed)
        self.assertIn("evidence gate", " ".join(result.rejected[0]["reasons"]))
        self.assertEqual(1, result.checks["evidence"]["rejected"])
        self.assertFalse(item.gate["exact_location_evidence"])

    def test_malformed_evidence_references_do_not_count(self):
        item = finding(evidence="unrelated", evidence_refs=[
            "not-a-dict", {"tool": "ast_analyze"}, {"evidence_id": "x"},
        ])
        result = self.gate.apply([item], self.parsed)
        self.assertEqual([], result.accepted)
        self.assertEqual(0, item.gate["strong_evidence_count"])

    def test_weak_tool_reference_is_not_enough_for_high_severity(self):
        item = finding(
            severity=Severity.HIGH, evidence="unrelated",
            evidence_refs=[{"evidence_id": "read_file:1", "tool": "read_file"}],
        )
        result = self.gate.apply([item], self.parsed)
        reasons = result.rejected[0]["reasons"]
        self.assertTrue(any("high-risk finding requires" in reason for reason in reasons))
        self.assertEqual(0, item.gate["strong_evidence_count"])

    def test_strong_tool_reference_accepts_a_high_severity_finding(self):
        item = finding(
            severity=Severity.HIGH, evidence="unrelated",
            evidence_refs=[{"evidence_id": "ast_analyze:1", "tool": "ast_analyze"}],
        )
        result = self.gate.apply([item], self.parsed)
        self.assertEqual([item], result.accepted)
        self.assertEqual(1, item.gate["strong_evidence_count"])

    def test_call_chain_satisfies_evidence_and_high_risk_requirements(self):
        item = finding(
            severity=Severity.CRITICAL, evidence="unrelated",
            call_chain=[{"caller": "handler", "callee": "eval"}],
        )
        result = self.gate.apply([item], self.parsed)
        self.assertEqual([item], result.accepted)
        self.assertFalse(item.gate["exact_location_evidence"])

    def test_confidence_gate_uses_the_configured_minimum(self):
        gate = FindingGate(minimum_confidence=0.9)
        item = finding(confidence=0.89)
        result = gate.apply([item], self.parsed)
        self.assertIn("confidence gate", " ".join(result.rejected[0]["reasons"]))
        self.assertEqual(0.9, result.checks["confidence"]["minimum"])
        self.assertEqual(1, result.checks["confidence"]["rejected"])

    def test_high_risk_findings_need_fix_and_test_guidance(self):
        for overrides in ({"fix": ""}, {"test": " "}):
            with self.subTest(overrides=overrides):
                item = finding(
                    severity=Severity.HIGH,
                    evidence_refs=[{"evidence_id": "ast_analyze:1", "tool": "ast_analyze"}],
                    **overrides,
                )
                result = self.gate.apply([item], self.parsed)
                self.assertIn("release gate", " ".join(result.rejected[0]["reasons"]))
                self.assertEqual(1, result.checks["release"]["rejected"])

    def test_medium_findings_do_not_require_fix_or_test_text(self):
        item = finding(fix="", test="")
        self.assertEqual([item], self.gate.apply([item], self.parsed).accepted)

    def test_counters_and_rejected_details_summarise_a_mixed_batch(self):
        accepted = finding()
        rejected = finding(confidence=0.1, path="other.py", evidence="nothing")
        result = self.gate.apply([accepted, rejected], self.parsed)
        self.assertEqual(1, result.checks["accepted"])
        self.assertEqual(1, result.checks["rejected"])
        self.assertEqual(1, result.checks["format"]["rejected"])
        self.assertEqual(1, result.checks["confidence"]["rejected"])
        detailed = result.rejected[0]
        self.assertEqual("SEC-EVAL", detailed["rule_id"])
        self.assertEqual("other.py", detailed["path"])
        self.assertTrue(detailed["reasons"])


if __name__ == "__main__":
    unittest.main()
