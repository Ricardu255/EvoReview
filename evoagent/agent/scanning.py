"""Rule scanners and diff-aware AST evidence."""
from .collaborators import ReviewerBackref
import ast
import hashlib
import json
import textwrap
import time
from typing import List

from ..diff_parser import ParsedDiff
from ..models import ComponentKind, Finding, Severity
from ..modes import component
from ..telemetry import ExecutionLedger


class ScanRunner(ReviewerBackref):
    def _scan(self, diff, parsed, ledger, scanners=None):
        started = time.monotonic()
        findings = self.rules.review(diff, parsed)
        ledger.record_tool(
            "agentic-scanner", "local-rule-scanner", {"added_lines": len(parsed.added_lines)},
            True, int((time.monotonic() - started) * 1000),
            {"findings": len(findings)},
        )
        scanners = list(scanners or [])
        for scanner in scanners:
            scanner_started = time.monotonic()
            scanner_name = self._scanner_name(scanner.name)
            try:
                scanned = scanner.review(diff, parsed)
            except Exception as exc:
                ledger.record_tool(
                    "agentic-scanner", scanner_name,
                    {"added_lines": len(parsed.added_lines)}, False,
                    int((time.monotonic() - scanner_started) * 1000), error=str(exc),
                )
                continue
            ledger.record_tool(
                "agentic-scanner", scanner_name, {"added_lines": len(parsed.added_lines)},
                True, int((time.monotonic() - scanner_started) * 1000),
                {"findings": len(scanned)},
            )
            for finding in scanned:
                if not finding.evidence_refs:
                    finding.evidence_refs = [{
                        "evidence_id": "scanner:%s:%s:%s" % (
                            finding.rule_id, finding.path, finding.line
                        ),
                        "tool": "declarative-scanner", "scanner": scanner_name,
                    }]
                if finding.source == "unknown":
                    finding.source = "declarative-scanner:%s" % scanner_name
            findings.extend(scanned)
        findings = self._merge(findings)
        ast_scans = self._attach_diff_ast_evidence(findings, parsed, ledger)
        return findings, {}, [
            component(ComponentKind.TOOL_SCANNER, "local-rule-scanner"),
        ] + [
            component(ComponentKind.TOOL_SCANNER, self._scanner_name(item.name))
            for item in scanners
        ] + ([component(ComponentKind.TOOL_SCANNER, "diff-ast-analyze")] if ast_scans else [])

    @staticmethod
    def _attach_diff_ast_evidence(
        findings: List[Finding], parsed: ParsedDiff, ledger: ExecutionLedger,
    ) -> int:
        lines = {(item.path, item.line): item.content for item in parsed.added_lines}
        scans = 0
        for finding in findings:
            if finding.severity not in {Severity.CRITICAL, Severity.HIGH}:
                continue
            source = lines.get((finding.path, finding.line), "")
            if not finding.path.endswith(".py") or not source.strip():
                continue
            started = time.monotonic()
            try:
                tree = ast.parse(textwrap.dedent(source))
                structures = [
                    type(node).__name__ for node in ast.walk(tree)
                    if isinstance(node, (ast.Call, ast.Assign, ast.AnnAssign, ast.keyword))
                ]
                supported = bool(structures)
                payload = {
                    "path": finding.path, "line": finding.line,
                    "valid_python_ast": True, "structures": structures,
                    "rule_id": finding.rule_id,
                }
            except SyntaxError as exc:
                supported = False
                payload = {
                    "path": finding.path, "line": finding.line,
                    "valid_python_ast": False, "error": str(exc),
                }
            ledger.record_tool(
                "agentic-scanner", "diff-ast-analyze",
                {"path": finding.path, "line": finding.line}, supported,
                int((time.monotonic() - started) * 1000), payload,
                "" if supported else payload.get("error", "no relevant AST structure"),
            )
            scans += 1
            if supported:
                rendered = json.dumps(payload, sort_keys=True)
                finding.evidence_refs.append({
                    "evidence_id": "diff-ast:%s" % hashlib.sha256(
                        rendered.encode("utf-8")
                    ).hexdigest()[:16],
                    "tool": "diff-ast-analyze", **payload,
                })
        return scans
