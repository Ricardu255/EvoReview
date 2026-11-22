"""Calibration chain: rule identity, confidence calibration, taxonomy gate,
scanner-priority merge and removed-line (line_side) support."""

import unittest

from evoagent.agent.findings import (
    _calibrated_confidence, _parse_findings, FindingPolicy,
)
from evoagent.domain.diff_parser import parse_unified_diff
from evoagent.domain.finding_identity import (
    extract_declared_rule_ids, normalize_worker_identity, worker_rule_catalog,
)
from evoagent.domain.gates import FindingGate
from evoagent.domain.models import Finding, Severity


DIFF = (
    "--- a/app.py\n"
    "+++ b/app.py\n"
    "@@ -1,2 +1,2 @@\n"
    "-old_secret = 'hunter2'\n"
    "+new_secret = 'hunter2'\n"
    " keep = 1\n"
)

PARSED = parse_unified_diff(DIFF)


def finding(**overrides):
    values = dict(
        rule_id="SEC-HARDCODED-SECRET", cwe="CWE-798",
        severity=Severity.HIGH, title="t", explanation="e",
        path="app.py", line=1, evidence="new_secret = 'hunter2'",
        fix="f", test="t", confidence=0.9, source="local-rule-scanner",
        sources=["local-rule-scanner"],
    )
    values.update(overrides)
    return Finding(**values)


class NormalizeWorkerIdentityTests(unittest.TestCase):
    def test_catalog_rules_use_the_program_cwe_mapping(self):
        rule, cwe, known = normalize_worker_identity(
            "sec-eval", "CWE-1", "security",
        )
        self.assertEqual("SEC-EVAL", rule)
        self.assertEqual("CWE-95", cwe)
        self.assertTrue(known)

    def test_role_boundaries_reject_cross_domain_rules(self):
        rule, _cwe, known = normalize_worker_identity(
            "SEC-EVAL", "", "correctness-reliability",
        )
        self.assertEqual("UNCLASSIFIED-CORRECTNESS", rule)
        self.assertFalse(known)

    def test_skill_declared_rules_are_accepted_without_cwe(self):
        declared = extract_declared_rule_ids(["use CODE-TEAM-POLICY here"])
        self.assertEqual({"CODE-TEAM-POLICY"}, declared)
        rule, cwe, known = normalize_worker_identity(
            "code-team-policy", "CWE-1", "security", declared,
        )
        self.assertEqual("CODE-TEAM-POLICY", rule)
        self.assertEqual("", cwe)  # custom rules never inherit model CWE
        self.assertTrue(known)

    def test_unknown_rules_fall_back_to_unclassified(self):
        rule, cwe, known = normalize_worker_identity("MADE-UP", "CWE-95", "security")
        self.assertEqual("UNCLASSIFIED-SECURITY", rule)
        self.assertEqual("", cwe)
        self.assertFalse(known)

    def test_worker_catalog_is_role_scoped(self):
        security = {item["rule_id"] for item in worker_rule_catalog("security")}
        reliability = {
            item["rule_id"] for item in worker_rule_catalog("correctness-reliability")
        }
        self.assertIn("SEC-EVAL", security)
        self.assertNotIn("REL-EMPTY-EXCEPT", security)
        self.assertIn("REL-EMPTY-EXCEPT", reliability)
        # Shared database rules are visible to both roles.
        self.assertIn("DB-DATA-INTEGRITY", security)
        self.assertIn("DB-DATA-INTEGRITY", reliability)


class CalibratedConfidenceTests(unittest.TestCase):
    def test_baseline_only(self):
        self.assertEqual(0.4, _calibrated_confidence(False, False, [], []))

    def test_strong_tool_and_chain_stack_to_the_cap(self):
        refs = [{"tool": "ast_analyze", "evidence_id": "e"}]
        # 0.40 base + 0.10 exact line + 0.08 rule + 0.08 refs + 0.15 strong
        # tool + 0.15 chain = 0.96 -> the 0.95 cap.
        self.assertEqual(0.95, _calibrated_confidence(True, True, refs, [{"a": 1}]))
        self.assertEqual(0.95, _calibrated_confidence(
            True, True, refs * 3, [{"a": 1}, {"b": 2}],
        ))
        # A weak-only tool reference keeps the plain refs bonus.
        self.assertEqual(0.66, _calibrated_confidence(
            True, True, [{"tool": "search_diff", "evidence_id": "e"}], [],
        ))


class ParseFindingsLineSideTests(unittest.TestCase):
    def test_new_and_old_lines_are_both_valid_locations(self):
        result = {"findings": [
            {"rule_id": "SEC-HARDCODED-SECRET", "path": "app.py", "line": 1,
             "evidence": "new_secret = 'hunter2'", "severity": "high"},
            {"rule_id": "SEC-HARDCODED-SECRET", "path": "app.py", "line": 1,
             "line_side": "old", "evidence": "old_secret = 'hunter2'",
             "severity": "low"},
        ]}
        findings = _parse_findings(result, PARSED, "security")
        self.assertEqual(
            ["new", "old"], [item.line_side for item in findings],
        )
        # Removed-line findings carry calibrated confidence like any other.
        self.assertEqual(0.58, findings[0].confidence)
        self.assertEqual(0.58, findings[1].confidence)


class MergeArbitrationTests(unittest.TestCase):
    def test_scanner_record_wins_over_higher_confidence_worker(self):
        scanner = finding(confidence=0.9)
        worker = finding(
            confidence=0.99, source="security", sources=["security"],
            evidence="different wording",
        )
        merged = FindingPolicy._merge([worker, scanner])
        self.assertEqual(1, len(merged))
        self.assertEqual("local-rule-scanner", merged[0].source)
        # Independent corroboration adds the capped bonus only.
        self.assertEqual(0.93, merged[0].confidence)
        self.assertEqual(["local-rule-scanner", "security"], merged[0].sources)

    def test_non_scanner_records_compete_by_confidence(self):
        first = finding(source="security", sources=["security"], confidence=0.5)
        second = finding(source="critic", sources=["critic"], confidence=0.7)
        merged = FindingPolicy._merge([first, second])
        self.assertEqual(0.73, merged[0].confidence)  # 0.7 + 0.03 corroboration

    def test_line_side_is_part_of_the_merge_key(self):
        new_side = finding(line_side="new", confidence=0.9)
        old_side = finding(line_side="old", confidence=0.4)
        merged = FindingPolicy._merge([new_side, old_side])
        self.assertEqual(2, len(merged))


class TaxonomyGateTests(unittest.TestCase):
    def setUp(self):
        self.gate = FindingGate()

    def test_consistent_cwe_passes(self):
        item = finding(severity=Severity.MEDIUM)
        result = self.gate.apply([item], PARSED)
        self.assertEqual([item], result.accepted)
        self.assertEqual(0, result.checks["taxonomy"]["rejected"])

    def test_conflicting_and_malformed_cwe_are_rejected(self):
        conflicting = finding(cwe="CWE-89")
        malformed = finding(cwe="CWE-abc")
        result = self.gate.apply([conflicting, malformed], PARSED)
        self.assertEqual([], result.accepted)
        self.assertIn(
            "taxonomy gate: CWE conflicts with program rule mapping",
            result.rejected[0]["reasons"],
        )
        self.assertIn(
            "taxonomy gate: malformed CWE", result.rejected[1]["reasons"],
        )
        self.assertEqual(2, result.checks["taxonomy"]["rejected"])

    def test_removed_lines_are_valid_gate_locations(self):
        item = finding(
            line_side="old", evidence="old_secret = 'hunter2'",
            severity=Severity.MEDIUM,
        )
        result = self.gate.apply([item], PARSED)
        self.assertEqual([item], result.accepted)
        self.assertTrue(item.gate["exact_location_evidence"])


if __name__ == "__main__":
    unittest.main()
