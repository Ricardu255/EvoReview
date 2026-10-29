import re
from typing import Any, Dict


MAX_FIELD_CHARS = 4000
MAX_EVIDENCE_CHARS = 4000
MAX_RENDERED_FINDINGS = 50
# GitHub usernames/teams: avoid notifying arbitrary people named by model output.
_MENTION = re.compile(r"(?<![\w`])@([A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?)")
_HTML_TAG = re.compile(r"<(?=/?[A-Za-z][^>]*>)")
_CODE_FENCE = re.compile(r"`{3,}")
_TILDE_FENCE = re.compile(r"~{3,}")


def _sanitize_text(value: Any, limit: int = MAX_FIELD_CHARS) -> str:
    """Render untrusted model/user text safely inside a GitHub comment."""
    text = "" if value is None else str(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Neutralize fence delimiters, mentions and embedded HTML/images so the
    # comment cannot escape its own markdown block or trigger notifications.
    text = _CODE_FENCE.sub(
        lambda match: match.group(0).replace("`", "`​"), text
    )
    text = _TILDE_FENCE.sub(
        lambda match: match.group(0).replace("~", "~​"), text
    )
    text = _MENTION.sub(lambda match: "@​" + match.group(1), text)
    text = text.replace("![", "!\\[")
    text = _HTML_TAG.sub("&lt;", text)
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def _sanitize_evidence(value: Any) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # The evidence is placed inside a text fence: a long backtick run would
    # otherwise terminate it early.
    text = _CODE_FENCE.sub("``", text)
    text = _TILDE_FENCE.sub("~~", text)
    text = text.rstrip("`\n")
    if len(text) > MAX_EVIDENCE_CHARS:
        text = text[:MAX_EVIDENCE_CHARS].rstrip() + "\n… (truncated)"
    return text


def _inline(value: Any, limit: int = 200) -> str:
    """Single-line value for a markdown code span."""
    return str("" if value is None else value).replace("`", "'").replace(
        "\n", " "
    )[:limit]


def to_markdown(report: Dict[str, Any]) -> str:
    title = "# EvoReview PR Review"
    if report.get("pull_request") is not None:
        title += " — #%s" % _inline(report["pull_request"], 20)
    lines = [
        title,
        "",
        "**Repository:** `%s`  " % _inline(report.get("repository", "")),
        "**Risk:** `%s`  " % _inline(report.get("risk", "unknown"), 40),
        "**Reviewer:** `%s`" % _inline(report.get("reviewer", "unknown"), 80),
        "",
        _sanitize_text(report.get("summary", "")),
        "",
    ]
    run_mode = report.get("run_mode") or {}
    execution = report.get("execution") or {}
    if run_mode:
        lines.extend([
            "## Execution facts",
            "",
            "- Mode: requested `%s`, effective `%s`" % (
                _inline(run_mode.get("requested", "unknown"), 40),
                _inline(run_mode.get("effective", "unknown"), 40),
            ),
            "- Model calls: `%s`; tool calls: `%s`" % (
                execution.get("llm_calls", 0), execution.get("tool_calls", 0)
            ),
            "- Tokens: input `%s`, output `%s`, total `%s`" % (
                execution.get("input_tokens", 0), execution.get("output_tokens", 0),
                execution.get("total_tokens", 0),
            ),
            "- Token cost: `$%.8f`; latency: `%s ms`" % (
                float(execution.get("cost_usd", 0) or 0), execution.get("duration_ms", 0)
            ),
            "",
        ])
        if run_mode.get("fallback_reason"):
            lines.extend([
                "> %s" % _sanitize_text(run_mode["fallback_reason"], 1000), ""
            ])
        context = execution.get("context_management") or {}
        if context:
            memory = context.get("memory_recall") or {}
            lines[-1:-1] = [
                "- Context compression: `%s` view(s), estimated reduction `%.1f%%`; "
                "recalled memories `%s`" % (
                    context.get("compression_calls", 0),
                    max(0.0, float(context.get("estimated_reduction_ratio", 0) or 0) * 100),
                    memory.get("recalled", 0),
                )
            ]
    collaboration = report.get("collaboration") or {}
    if collaboration and run_mode.get("effective") == "agentic" and execution.get("llm_calls", 0):
        lines.extend([
            "## LLM Agent collaboration",
            "",
            "- Protocol: `%s`" % _inline(collaboration.get("protocol", "unknown"), 80),
            "- Actual roles: `%s`" % ", ".join(
                _inline(role, 60) for role in collaboration.get("roles") or []
            ),
            "- Candidate findings: `%s`; critic decisions: `%s`" % (
                collaboration.get("candidate_findings", 0),
                len(collaboration.get("critic_decisions") or []),
            ),
            "",
        ])
    findings = report.get("findings", [])
    if not findings:
        lines.append("✅ No actionable issue detected in the added lines.")
        return "\n".join(lines) + "\n"
    lines.extend(["## Findings", ""])
    icons = {"critical": "🚨", "high": "🔴", "medium": "🟠", "low": "🟡"}
    for index, item in enumerate(findings[:MAX_RENDERED_FINDINGS], 1):
        severity = _inline(item.get("severity", "medium"), 40)
        lines.extend(
            [
                "### %d. %s %s" % (
                    index, icons.get(severity, "•"),
                    _inline(item.get("title", "Finding"), 200),
                ),
                "",
                "`%s:%s` · **%s** · `%s`" % (
                    _inline(item.get("path", "")), _inline(item.get("line", 0), 20),
                    severity.upper(), _inline(item.get("rule_id", ""), 80),
                ),
                "",
                _sanitize_text(item.get("explanation", "")),
                "",
                "**Evidence**",
                "",
                "```text",
                _sanitize_evidence(item.get("evidence", "")),
                "```",
                "",
                "**Evidence references:** %s" % (
                    ", ".join(
                        "`%s`" % _inline(ref.get("evidence_id", ""), 80)
                        for ref in item.get("evidence_refs", []) if ref.get("evidence_id")
                    ) or "none"
                ),
                "",
                "**Suggested fix:** %s" % _sanitize_text(item.get("fix", "")),
                "",
                "**Suggested test:** %s" % _sanitize_text(item.get("test", "")),
                "",
            ]
        )
    remaining = len(findings) - MAX_RENDERED_FINDINGS
    if remaining > 0:
        lines.extend(["_%d additional finding(s) omitted._" % remaining, ""])
    return "\n".join(lines) + "\n"
