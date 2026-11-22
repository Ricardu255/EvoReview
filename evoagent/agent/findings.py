"""Finding parsing, merging and Critic/Lead decision application.

Confidence is calibrated by the program from verifiable evidence instead of
trusting model self-reports, deterministic scanner records win merges, and
Critic/Lead adjustments are capped so a model cannot inflate a finding past
the gates.  Ported from the parallel EvoAgnet branch.
"""
import json
from typing import Dict, Iterable, List, Tuple

from ..domain.diff_parser import ParsedDiff
from ..domain.finding_identity import (
    RULE_CATALOG, canonical_identity, normalize_worker_identity,
)
from ..domain.gates import FindingGate
from ..domain.models import Finding, Severity


def _collect_evidence(observations: List[dict]) -> Dict[str, dict]:
    values = {}
    for item in observations:
        result = item.get("result")
        if isinstance(result, dict) and result.get("evidence_id"):
            values[str(result["evidence_id"])] = {
                "evidence_id": result["evidence_id"],
                "tool": result.get("tool", item.get("tool", "")),
                "output_preview": json.dumps(
                    result.get("output"), ensure_ascii=False, default=str
                )[:2000],
            }
    return values


def _strict_bool(value) -> bool:
    """Interpret a model-provided boolean; the string "false" must not be truthy."""
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1"}
    return bool(value)


# Confidence comes from verifiable evidence, never from the model's self-score,
# so a fabricated high score cannot bypass the confidence gate.
def _calibrated_confidence(
    exact_line: bool, known_rule: bool, refs: List[dict], chain: List[dict],
) -> float:
    score = 0.40
    if exact_line:
        score += 0.10
    if known_rule:
        score += 0.08
    if refs:
        score += 0.08
    if any(str(item.get("tool")) in FindingGate.STRONG_EVIDENCE_TOOLS for item in refs):
        score += 0.15
    if chain:
        score += 0.15
    return round(min(0.95, score), 2)


def _parse_findings(
    result: dict, parsed: ParsedDiff, role: str,
    allowed_custom_rules: Tuple[str, ...] = (),
) -> List[Finding]:
    valid = {("new", item.path, item.line): item.content for item in parsed.added_lines}
    valid.update({("old", item.path, item.line): item.content for item in parsed.removed_lines})
    evidence = _collect_evidence(result.get("_observations") or [])
    findings = []
    for raw in result.get("findings") or []:
        if not isinstance(raw, dict):
            continue
        try:
            path, line = str(raw.get("path", "")), int(raw.get("line", 0))
        except (TypeError, ValueError):
            continue
        side = str(raw.get("line_side", "new"))
        location = (side, path, line)
        if location not in valid:
            continue
        try:
            severity = Severity(str(raw.get("severity", "medium")).lower())
        except ValueError:
            severity = Severity.MEDIUM
        refs = [
            evidence[str(item)] for item in raw.get("evidence_ids") or []
            if str(item) in evidence
        ]
        chain = [item for item in (raw.get("call_chain") or []) if isinstance(item, dict)][:20]
        rule_id, cwe, known_rule = normalize_worker_identity(
            raw.get("rule_id"), raw.get("cwe"), role, allowed_custom_rules,
        )
        quoted = str(raw.get("evidence", ""))[:500]
        exact_line = bool(quoted.strip() and quoted.strip() in valid[location].strip())
        findings.append(Finding(
            rule_id=rule_id, cwe=cwe or None,
            severity=severity, title=str(raw.get("title", "Review finding"))[:200],
            explanation=str(raw.get("explanation", ""))[:4000], path=path, line=line,
            line_side=side, evidence=quoted,
            fix=str(raw.get("fix", ""))[:4000], test=str(raw.get("test", ""))[:4000],
            confidence=_calibrated_confidence(exact_line, known_rule, refs, chain),
            evidence_refs=refs, call_chain=chain, source=role, sources=[role],
        ))
    return findings


class FindingPolicy:
    @staticmethod
    def _apply_critic(result, candidates):
        evidence = _collect_evidence(result.get("_observations") or [])
        by_index = {
            int(item.get("finding_index")): item
            for item in result.get("decisions") or []
            if isinstance(item, dict) and str(item.get("finding_index", "")).isdigit()
        }
        decisions = []
        for index, finding in enumerate(candidates):
            decision = by_index.get(index)
            accepted = bool(decision and _strict_bool(decision.get("accepted")))
            if decision:
                try:
                    adjustment = float(decision.get("confidence_adjustment", 0))
                except (TypeError, ValueError):
                    adjustment = 0.0
                supporting = [
                    evidence[str(value)]
                    for value in decision.get("supporting_evidence_ids") or []
                    if str(value) in evidence
                ]
                finding.evidence_refs.extend(supporting)
                # Negative review may lower the score; positive review only
                # adds a small, capped bump when backed by real tool evidence.
                upper = 0.08 if supporting else 0.0
                adjustment = max(-0.15, min(upper, adjustment))
                finding.confidence = max(0.0, min(1.0, finding.confidence + adjustment))
            decisions.append({
                "finding_index": index, "accepted": accepted,
                "objections": (decision or {}).get(
                    "objections", ["critic returned no explicit decision"]
                ),
            })
        return candidates, decisions

    @staticmethod
    def _apply_lead_final(decision, candidates):
        raw_indices = decision.get("accepted_finding_indices") or []
        accepted_indices = {
            int(value) for value in raw_indices if str(value).isdigit()
            and 0 <= int(value) < len(candidates)
        }
        adjustments = {
            int(item.get("finding_index")): item.get("adjustment", 0)
            for item in decision.get("confidence_adjustments") or []
            if isinstance(item, dict) and str(item.get("finding_index", "")).isdigit()
        }
        accepted = []
        for index in sorted(accepted_indices):
            finding = candidates[index]
            try:
                adjustment = float(adjustments.get(index, 0))
            except (TypeError, ValueError):
                adjustment = 0.0
            # The Lead curates; it never raises evidence confidence on taste.
            adjustment = max(-0.10, min(0.0, adjustment))
            finding.confidence = max(0.0, min(1.0, finding.confidence + adjustment))
            accepted.append(finding)
        return accepted

    @staticmethod
    def _restore_findings(values):
        findings = []
        for value in values or []:
            try:
                severity = Severity(str(value.get("severity", "medium")))
                raw_cwe = value.get("cwe")
                findings.append(Finding(
                    rule_id=str(value.get("rule_id", "REVIEW")), severity=severity,
                    cwe=(str(raw_cwe).strip().upper() or None) if raw_cwe else None,
                    title=str(value.get("title", "Review finding")),
                    explanation=str(value.get("explanation", "")),
                    path=str(value.get("path", "")), line=int(value.get("line", 0)),
                    line_side=str(value.get("line_side", "new")),
                    evidence=str(value.get("evidence", "")), fix=str(value.get("fix", "")),
                    test=str(value.get("test", "")), confidence=float(value.get("confidence", 0.7)),
                    evidence_refs=list(value.get("evidence_refs") or []),
                    call_chain=list(value.get("call_chain") or []),
                    source=str(value.get("source", "unknown")),
                    sources=list(value.get("sources") or []),
                ))
            except (TypeError, ValueError):
                continue
        return findings

    @staticmethod
    def _public_decision(result):
        return {
            key: value for key, value in result.items()
            if not str(key).startswith("_")
        }

    @staticmethod
    def _merge(findings: Iterable[Finding]) -> List[Finding]:
        merged = {}
        for finding in findings:
            rule = str(finding.rule_id or "").strip().upper()
            identity = (
                "RULE:" + rule
                if rule in RULE_CATALOG or rule.startswith("UNCLASSIFIED-") else
                canonical_identity(rule, finding.cwe)
            )
            key = (
                finding.path, getattr(finding, "line_side", "new"), finding.line, identity,
            )
            current = merged.get(key)
            if current is None:
                if not finding.sources and finding.source:
                    finding.sources = [finding.source]
                merged[key] = finding
                continue

            def provenance(item):
                values = list(item.sources or [])
                if item.source and item.source not in values:
                    values.append(item.source)
                return values

            def deterministic(item):
                return any("scanner" in value for value in provenance(item))

            # Worker self-scores and wording cannot override scanner facts;
            # only records without a scanner source compete by calibrated score.
            if deterministic(finding) and not deterministic(current):
                primary, secondary = finding, current
            elif deterministic(current) and not deterministic(finding):
                primary, secondary = current, finding
            elif finding.confidence > current.confidence:
                primary, secondary = finding, current
            else:
                primary, secondary = current, finding

            primary_sources = provenance(primary)
            secondary_sources = provenance(secondary)
            sources = []
            for value in primary_sources + secondary_sources:
                if value and value not in sources:
                    sources.append(value)
            primary.sources = sources

            seen_refs = {
                (str(item.get("evidence_id", "")), str(item.get("tool", "")))
                for item in primary.evidence_refs if isinstance(item, dict)
            }
            for item in secondary.evidence_refs:
                marker = (
                    str(item.get("evidence_id", "")), str(item.get("tool", ""))
                ) if isinstance(item, dict) else ("", "")
                if marker not in seen_refs:
                    primary.evidence_refs.append(item)
                    seen_refs.add(marker)

            seen_chain = {json.dumps(item, sort_keys=True) for item in primary.call_chain}
            for item in secondary.call_chain:
                marker = json.dumps(item, sort_keys=True)
                if marker not in seen_chain:
                    primary.call_chain.append(item)
                    seen_chain.add(marker)

            # An independent source adds a small, capped corroboration bonus;
            # it never inherits the secondary record's self-declared confidence.
            corroborated = any(value not in primary_sources for value in secondary_sources)
            if corroborated:
                primary.confidence = round(min(0.98, primary.confidence + 0.03), 2)
            merged[key] = primary
        order = {Severity.CRITICAL: 0, Severity.HIGH: 1, Severity.MEDIUM: 2, Severity.LOW: 3}
        return sorted(merged.values(), key=lambda item: (order[item.severity], item.path, item.line))

    @staticmethod
    def _scanner_name(name: str) -> str:
        value = str(name)
        return value[:-5] + "scanner" if value.endswith("-agent") else value
