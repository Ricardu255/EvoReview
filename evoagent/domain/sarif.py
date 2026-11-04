"""Convert a review report into SARIF 2.1.0 for CI/code-scanning consumers."""
from typing import Any, Dict

from .report import redact_secret_evidence


SCHEMA_URL = "https://json.schemastore.org/sarif-2.1.0.json"
LEVELS = {
    "critical": "error",
    "high": "error",
    "medium": "warning",
    "low": "note",
}


def to_sarif(report: Dict[str, Any], tool_version: str = "0.4.0") -> Dict[str, Any]:
    findings = report.get("findings") or []
    results = []
    rules = {}
    for finding in findings:
        rule_id = str(finding.get("rule_id", "REVIEW"))
        severity = str(finding.get("severity", "medium")).lower()
        rule = rules.setdefault(rule_id, {
            "id": rule_id,
            "name": rule_id,
            "shortDescription": {"text": str(finding.get("title", rule_id))[:200]},
            "help": {"text": str(finding.get("explanation", ""))[:4000]},
            "properties": {
                "tags": ["security", "code-review"],
                "defaultConfiguration": {"level": LEVELS.get(severity, "warning")},
            },
        })
        evidence = redact_secret_evidence(
            finding.get("rule_id", ""), finding.get("evidence", ""),
        )
        result = {
            "ruleId": rule_id,
            "level": LEVELS.get(severity, "warning"),
            "message": {
                "text": ("%s: %s" % (
                    finding.get("title", "Review finding"),
                    finding.get("explanation", ""),
                ))[:4000],
            },
            "locations": [{
                "physicalLocation": {
                    "artifactLocation": {"uri": str(finding.get("path", ""))},
                    "region": {
                        "startLine": max(1, int(finding.get("line", 1) or 1)),
                        "endLine": max(1, int(finding.get("line", 1) or 1)),
                    },
                }
            }],
            "properties": {
                "severity": severity,
                "confidence": finding.get("confidence"),
                "cwe": finding.get("cwe"),
                "fix": finding.get("fix", ""),
                "test": finding.get("test", ""),
                "evidence": evidence,
            },
        }
        if rule is not None:
            result["ruleIndex"] = sorted(rules).index(rule_id)
        results.append(result)
    return {
        "$schema": SCHEMA_URL,
        "version": "2.1.0",
        "runs": [{
            "tool": {
                "driver": {
                    "name": "EvoReview",
                    "version": tool_version,
                    "informationUri": "https://github.com/Ricardu255/EvoReview",
                    "rules": [rules[key] for key in sorted(rules)],
                }
            },
            "invocations": [{
                "executionSuccessful": True,
                "properties": {
                    "repository": report.get("repository", ""),
                    "pullRequest": report.get("pull_request"),
                    "risk": report.get("risk", "unknown"),
                },
            }],
            "results": results,
        }],
    }
