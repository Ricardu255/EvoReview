"""SARIF 2.1.0 conversion tests."""
import unittest

from evoagent.domain.sarif import to_sarif


REPORT = {
    "repository": "org/repo", "pull_request": 12, "summary": "summary",
    "risk": "high", "reviewer": "test",
    "findings": [
        {
            "rule_id": "SEC-EVAL", "severity": "critical",
            "title": "Dynamic execution", "explanation": "Input reaches eval.",
            "path": "app.py", "line": 7, "evidence": "eval(user_input)",
            "fix": "Use a parser.", "test": "Add a test.",
            "confidence": 0.9, "cwe": "CWE-95",
        },
        {
            "rule_id": "REL-DEBUG-PRINT", "severity": "low",
            "title": "Debug print", "explanation": "Remove it.",
            "path": "app.py", "line": 4, "evidence": "print(x)",
            "fix": "Remove it.", "test": "Check logs.",
            "confidence": 0.8,
        },
    ],
}


class SarifTests(unittest.TestCase):
    def test_converts_findings_to_sarif_results(self):
        value = to_sarif(REPORT, "0.4.0")
        self.assertEqual("2.1.0", value["version"])
        self.assertIn("sarif", value["$schema"])
        run = value["runs"][0]
        self.assertEqual("EvoReview", run["tool"]["driver"]["name"])
        self.assertEqual(2, len(run["results"]))
        first = run["results"][0]
        self.assertEqual("SEC-EVAL", first["ruleId"])
        self.assertEqual("error", first["level"])
        self.assertEqual("app.py", first["locations"][0]["physicalLocation"]
                         ["artifactLocation"]["uri"])
        self.assertEqual(7, first["locations"][0]["physicalLocation"]
                         ["region"]["startLine"])
        self.assertEqual("note", run["results"][1]["level"])
        self.assertEqual(2, len(run["tool"]["driver"]["rules"]))

    def test_empty_report_has_a_valid_empty_run(self):
        value = to_sarif({
            "repository": "org/repo", "pull_request": None,
            "summary": "clean", "risk": "low", "findings": [],
        })
        self.assertEqual([], value["runs"][0]["results"])
        self.assertEqual(
            [], value["runs"][0]["tool"]["driver"]["rules"],
        )

    def test_hardcoded_secret_evidence_is_redacted(self):
        value = to_sarif({
            "repository": "org/repo", "pull_request": 1,
            "summary": "secret", "risk": "high",
            "findings": [{
                "rule_id": "SEC-HARDCODED-SECRET", "severity": "high",
                "title": "Secret", "explanation": "Hardcoded.",
                "path": "config.py", "line": 1,
                "evidence": "password = 'super-secret'",
                "fix": "Use env.", "test": "Remove secret.",
            }],
        })
        evidence = value["runs"][0]["results"][0]["properties"]["evidence"]
        self.assertNotIn("super-secret", evidence)
        self.assertIn("***", evidence)


if __name__ == "__main__":
    unittest.main()
