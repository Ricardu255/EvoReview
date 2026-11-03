"""Tests for small agent modules: lazy exports, memory hooks, findings and loops."""
import json
import threading
import unittest

import evoagent.agent as agent_package
from evoagent.agent.bounded_role import BoundedRole
from evoagent.agent.context_manager import ContextManager
from evoagent.agent.findings import FindingPolicy, _collect_evidence, _parse_findings, _strict_bool
from evoagent.agent.memory_hooks import MemoryHooks
from evoagent.agent.runtime import AgentTool, RuntimeBudgetExceeded, ToolRegistry
from evoagent.agent.scanning import ScanRunner
from evoagent.agent.telemetry import ExecutionLedger
from evoagent.domain.diff_parser import parse_unified_diff
from evoagent.domain.models import Finding, Severity


DIFF = "--- a/app.py\n+++ b/app.py\n@@ -0,0 +1 @@\n+result = eval(raw)\n"
PARSED = parse_unified_diff(DIFF)


def finding(**overrides):
    values = dict(
        rule_id="SEC-EVAL", severity=Severity.HIGH, title="t", explanation="e",
        path="app.py", line=1, evidence="result = eval(raw)",
        fix="fix", test="test", confidence=0.8,
    )
    values.update(overrides)
    return Finding(**values)


class AgentPackageTests(unittest.TestCase):
    def test_agentic_reviewer_is_exported_lazily(self):
        from evoagent.agent.reviewer import AgenticReviewer
        self.assertIs(AgenticReviewer, agent_package.AgenticReviewer)

    def test_unknown_attribute_raises(self):
        with self.assertRaises(AttributeError):
            agent_package.does_not_exist


class MemoryHooksTests(unittest.TestCase):
    def _runtime(self, memories=None, manager=True):
        class Manager:
            def __init__(self):
                self.recalls = []
                self.observations = []
                self.remembered = []
                self.consolidated = []
                self.fail = False

            def recall_working(self, tenant, repository, task, limit, agent):
                self.recalls.append((tenant, repository, task, limit, agent))
                return memories or []

            def remember_observation(self, *args):
                self.observations.append(args)

            def remember_finding(self, *args):
                self.remembered.append(args)

            def consolidate_task(self, *args):
                if self.fail:
                    raise RuntimeError("memory down")
                self.consolidated.append(args)

        class Context:
            def format_memories(self, values):
                return {"context": list(values)}

        class Runtime:
            def __init__(self):
                self.memory_scope_lock = threading.Lock()
                self.memory_scopes = {"task-1": ("tenant-a", "org/repo")}
                self.memory_manager = Manager() if manager else None
                self.context_manager = Context()

        return Runtime()

    def test_supplier_returns_scoped_memories_with_a_trust_warning(self):
        runtime = self._runtime(memories=[{"observation": "x"}])
        hooks = MemoryHooks(runtime)
        supplier, _sink = hooks._memory_hooks("task-1", "lead")
        context = supplier()
        self.assertEqual({"context": [{"observation": "x"}],
                          "trust": "untrusted, task-scoped tool observations; verify before making a claim"},
                         context)
        self.assertEqual(
            ("tenant-a", "org/repo", "task-1", 12, ""), runtime.memory_manager.recalls[0],
        )

    def test_workers_only_recall_their_own_role(self):
        runtime = self._runtime(memories=[{"observation": "x"}])
        supplier, _sink = MemoryHooks(runtime)._memory_hooks("task-1", "security")
        supplier()
        self.assertEqual("security", runtime.memory_manager.recalls[0][-1])

    def test_supplier_is_empty_without_manager_scope_or_memories(self):
        empty = self._runtime(memories=[])
        supplier, _ = MemoryHooks(empty)._memory_hooks("task-1", "lead")
        self.assertIsNone(supplier())

        no_manager = self._runtime(memories=[{"x": 1}], manager=False)
        supplier, sink = MemoryHooks(no_manager)._memory_hooks("task-1", "lead")
        self.assertIsNone(supplier())
        sink("lead", {"observation": "x"})

        scoped = self._runtime(memories=[{"x": 1}])
        supplier, sink = MemoryHooks(scoped)._memory_hooks("unknown-task", "lead")
        self.assertIsNone(supplier())
        sink("lead", {"observation": "x"})
        self.assertEqual([], scoped.memory_manager.observations)

    def test_sink_records_observations(self):
        runtime = self._runtime()
        _supplier, sink = MemoryHooks(runtime)._memory_hooks("task-1", "lead")
        sink("lead", {"observation": "x"})
        self.assertEqual(
            [("tenant-a", "org/repo", "task-1", "lead", {"observation": "x"})],
            runtime.memory_manager.observations,
        )

    def test_persist_task_memory_writes_findings_and_consolidates(self):
        runtime = self._runtime()
        hooks = MemoryHooks(runtime)
        finding_item = finding(gate={"passed": True})
        gated = type("Gated", (), {
            "accepted": [finding_item],
            "rejected": [{"rule_id": "X"}],
            "checks": {"accepted": 1},
        })()
        hooks._persist_task_memory(
            "task-1", "tenant-a", "org/repo", [finding_item], gated,
            ["app.py"], {"roles": ["lead", "security"]},
        )
        manager = runtime.memory_manager
        self.assertEqual(1, len(manager.remembered))
        self.assertEqual(1, len(manager.consolidated))
        payload = manager.consolidated[0][-1]
        self.assertEqual(["app.py"], payload["files"])
        self.assertEqual(["lead", "security"], payload["agent_roles"])
        self.assertEqual("SEC-EVAL", payload["accepted_findings"][0]["rule_id"])
        self.assertEqual(0.8, payload["accepted_findings"][0]["confidence"])

    def test_persist_task_memory_swallows_errors_and_missing_manager(self):
        runtime = self._runtime()
        runtime.memory_manager.fail = True
        gated = type("Gated", (), {"accepted": [], "rejected": [], "checks": {}})()
        MemoryHooks(runtime)._persist_task_memory(
            "t", "tenant", "repo", [], gated, [], {},
        )  # must not raise

        no_manager = self._runtime(manager=False)
        MemoryHooks(no_manager)._persist_task_memory(
            "t", "tenant", "repo", [], gated, [], {},
        )


class FindingsParsingTests(unittest.TestCase):
    def test_collect_evidence_filters_and_previews(self):
        values = _collect_evidence([
            {"tool": "fallback", "result": {"evidence_id": "e1", "output": {"a": 1}}},
            {"result": {"evidence_id": "e2", "tool": "ast_analyze", "output": "x" * 3000}},
            {"result": {"output": "no id"}},
            {"result": "not-a-dict"},
        ])
        self.assertEqual({"e1", "e2"}, set(values))
        self.assertEqual("fallback", values["e1"]["tool"])
        self.assertEqual("ast_analyze", values["e2"]["tool"])
        self.assertLessEqual(len(values["e2"]["output_preview"]), 2000)

    def test_strict_bool_parses_strings(self):
        for value in ("true", " YES ", "1"):
            self.assertTrue(_strict_bool(value))
        for value in ("false", "no", "0", "", None):
            self.assertFalse(_strict_bool(value))
        self.assertTrue(_strict_bool(1))

    def test_parse_findings_normalises_and_rejects_bad_locations(self):
        result = {
            "_observations": [{"result": {
                "evidence_id": "e1", "tool": "ast_analyze", "output": {},
            }}],
            "findings": [
                {"path": "app.py", "line": "not-a-line"},
                {"path": "other.py", "line": 1},
                {"path": "app.py", "line": 1, "severity": "urgent",
                 "confidence": "high", "evidence_ids": ["e1", "missing"],
                 "call_chain": [{"a": 1}, "bad"]},
            ],
        }
        findings = _parse_findings(result, PARSED, "security")
        self.assertEqual(1, len(findings))
        item = findings[0]
        self.assertEqual(Severity.MEDIUM, item.severity)
        self.assertEqual(0.7, item.confidence)
        self.assertEqual("security", item.source)
        self.assertEqual("e1", item.evidence_refs[0]["evidence_id"])
        self.assertEqual([{"a": 1}], item.call_chain)

    def test_parse_findings_clamps_confidences(self):
        for value, expected in ((2.5, 1.0), (-1, 0.0)):
            result = {"findings": [{
                "path": "app.py", "line": 1, "confidence": value,
            }]}
            self.assertEqual(expected, _parse_findings(result, PARSED, "r")[0].confidence)


class FindingPolicyTests(unittest.TestCase):
    def test_critic_applies_decisions_adjustments_and_evidence(self):
        candidates = [finding(confidence=0.5), finding(confidence=0.5)]
        result = {
            "_observations": [{"result": {
                "evidence_id": "e1", "tool": "ast_analyze", "output": {},
            }}],
            "decisions": [
                {"finding_index": 0, "accepted": "true",
                 "confidence_adjustment": 0.2,
                 "supporting_evidence_ids": ["e1", "missing"]},
                {"finding_index": 1, "accepted": "false",
                 "confidence_adjustment": "bad"},
                {"finding_index": "not-a-number", "accepted": True},
            ],
        }
        accepted, decisions = FindingPolicy._apply_critic(result, candidates)
        self.assertTrue(decisions[0]["accepted"])
        self.assertEqual(0.7, candidates[0].confidence)
        self.assertEqual("e1", candidates[0].evidence_refs[0]["evidence_id"])
        self.assertFalse(decisions[1]["accepted"])
        self.assertEqual(
            ["critic returned no explicit decision"], decisions[1]["objections"],
        )
        self.assertEqual(2, len(accepted))

    def test_critic_adjustments_are_clamped(self):
        candidates = [finding(confidence=0.9)]
        FindingPolicy._apply_critic({
            "decisions": [{"finding_index": 0, "accepted": True,
                           "confidence_adjustment": 5}],
        }, candidates)
        self.assertEqual(1.0, candidates[0].confidence)

    def test_lead_final_filters_indices_and_applies_adjustments(self):
        candidates = [finding(confidence=0.5), finding(confidence=0.5)]
        accepted = FindingPolicy._apply_lead_final({
            "accepted_finding_indices": [1, "0", 5, "x", 0],
            "confidence_adjustments": [
                {"finding_index": 1, "adjustment": 0.2},
                {"finding_index": 0, "adjustment": "bad"},
                {"finding_index": "x", "adjustment": 1},
            ],
        }, candidates)
        self.assertEqual([candidates[0], candidates[1]], accepted)
        self.assertEqual(0.5, candidates[0].confidence)
        self.assertEqual(0.7, candidates[1].confidence)

    def test_restore_findings_skips_invalid_values(self):
        restored = FindingPolicy._restore_findings([
            {"rule_id": "R", "severity": "high", "path": "a.py", "line": 1},
            {"rule_id": "R", "severity": "urgent"},
            {"rule_id": "R", "severity": "low", "line": "bad"},
        ])
        self.assertEqual(1, len(restored))
        self.assertEqual(Severity.HIGH, restored[0].severity)

    def test_public_decision_removes_internal_keys(self):
        self.assertEqual({"a": 1}, FindingPolicy._public_decision({"a": 1, "_x": 2}))

    def test_merge_keeps_highest_confidence_and_sorts_by_severity(self):
        low = finding(rule_id="LOW", severity=Severity.LOW, line=1, confidence=0.9)
        critical = finding(rule_id="CRIT", severity=Severity.CRITICAL, line=1, confidence=0.4)
        duplicate = finding(rule_id="LOW", severity=Severity.LOW, line=1, confidence=0.5)
        merged = FindingPolicy._merge([low, critical, duplicate])
        self.assertEqual(["CRIT", "LOW"], [item.rule_id for item in merged])
        self.assertEqual(0.9, merged[1].confidence)

    def test_scanner_name_normalises_agent_suffix(self):
        self.assertEqual("security-scanner", FindingPolicy._scanner_name("security-agent"))
        self.assertEqual("semgrep", FindingPolicy._scanner_name("semgrep"))


class _ScriptedClient:
    def __init__(self, actions):
        self.actions = list(actions)
        self.calls = []

    def complete_json(self, role, prompt, user, ledger, max_tokens=None):
        self.calls.append((role, prompt, user, max_tokens))
        return self.actions.pop(0)


def registry():
    return ToolRegistry([
        AgentTool("echo", "Echo", {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"], "additionalProperties": False,
        }, lambda value: {"echo": value}),
    ])


class BoundedRoleTests(unittest.TestCase):
    def _role(self, actions, **overrides):
        values = dict(
            name="security", prompt="system", client=_ScriptedClient(actions),
            token_budget=10000, time_budget=10, max_steps=4,
            context_manager=ContextManager(),
        )
        values.update(overrides)
        return BoundedRole(**values)

    def test_final_action_returns_observations_and_steps(self):
        role = self._role([{"action": "final", "findings": []}])
        ledger = ExecutionLedger("review")
        result = role.run(json.dumps({"diff": DIFF}), registry(), ledger)
        self.assertEqual([], result["_observations"])
        self.assertEqual(1, result["_steps"])
        events = {item["event"] for item in ledger.agent_traces["security"]}
        self.assertIn("started", events)
        self.assertIn("finished", events)

    def test_nested_content_envelope_is_unwrapped(self):
        role = self._role([{"content": {"action": "final", "findings": []}}])
        result = role.run(json.dumps({"diff": DIFF}), registry(), ExecutionLedger("r"))
        self.assertEqual({"action": "final", "findings": []},
                         {key: value for key, value in result.items()
                          if not key.startswith("_")})

    def test_payload_without_action_is_treated_as_final(self):
        role = self._role([{"revision_requests": []}])
        result = role.run(json.dumps({"diff": DIFF}), registry(), ExecutionLedger("r"))
        self.assertEqual([], result["revision_requests"])

    def test_invalid_action_raises(self):
        role = self._role([{"action": "dance"}])
        with self.assertRaisesRegex(ValueError, "invalid action"):
            role.run(json.dumps({"diff": DIFF}), registry(), ExecutionLedger("r"))

    def test_tool_calls_are_recorded_and_errors_are_observations(self):
        role = self._role([
            {"action": "tool", "tool": "echo", "arguments": {"value": "hi"}},
            {"action": "tool", "tool": "missing", "arguments": {}},
            {"action": "final", "findings": []},
        ])
        ledger = ExecutionLedger("r")
        result = role.run(json.dumps({"diff": DIFF}), registry(), ledger)
        self.assertEqual(2, len(result["_observations"]))
        self.assertTrue(result["_observations"][0]["ok"])
        self.assertFalse(result["_observations"][1]["ok"])
        self.assertIn("unknown agent tool", result["_observations"][1]["error"])

    def test_step_budget_exhaustion(self):
        role = self._role([
            {"action": "tool", "tool": "echo", "arguments": {"value": "hi"}},
        ], max_steps=1)
        with self.assertRaisesRegex(RuntimeBudgetExceeded, "step budget"):
            role.run(json.dumps({"diff": DIFF}), registry(), ExecutionLedger("r"))

    def test_time_and_token_budgets_are_enforced(self):
        for overrides, message in (
            ({"time_budget": 0}, "budget exhausted"),
            ({"token_budget": 0}, "budget exhausted"),
        ):
            with self.subTest(overrides=overrides):
                role = self._role([{"action": "final"}], **overrides)
                with self.assertRaisesRegex(RuntimeBudgetExceeded, message):
                    role.run("{}", registry(), ExecutionLedger("r"))

    def test_working_memory_is_injected_and_failures_are_traced(self):
        role = self._role([{"action": "final", "findings": []}],
                          working_memory_supplier=lambda: {"note": "remembered"})
        ledger = ExecutionLedger("r")
        role.run(json.dumps({"diff": DIFF}), registry(), ledger)
        sent = json.loads(role.client.calls[0][2])
        task_context = json.loads(sent["task"])
        self.assertEqual({"note": "remembered"}, task_context["working_memory"])

        def broken():
            raise RuntimeError("no memory")

        role = self._role([{"action": "final", "findings": []}],
                          working_memory_supplier=broken)
        ledger = ExecutionLedger("r")
        role.run(json.dumps({"diff": DIFF}), registry(), ledger)
        self.assertTrue(any(
            item["event"] == "working_memory_unavailable"
            for item in ledger.agent_traces["security"]
        ))

    def test_observation_sink_failures_are_traced(self):
        def broken_sink(agent, observation):
            raise RuntimeError("sink down")

        role = self._role([
            {"action": "tool", "tool": "echo", "arguments": {"value": "hi"}},
            {"action": "final", "findings": []},
        ], observation_sink=broken_sink)
        ledger = ExecutionLedger("r")
        role.run(json.dumps({"diff": DIFF}), registry(), ledger)
        self.assertTrue(any(
            item["event"] == "working_memory_write_failed"
            for item in ledger.agent_traces["security"]
        ))


class _Rules:
    def __init__(self, findings):
        self._findings = findings

    def review(self, diff, parsed):
        return list(self._findings)


class _Scanner:
    def __init__(self, name, findings=None, error=None):
        self.name = name
        self._findings = findings or []
        self.error = error

    def review(self, diff, parsed):
        if self.error:
            raise self.error
        return [item for item in self._findings]


class _Runtime:
    def __init__(self, rules):
        self.rules = rules


def runner_for(findings, scanners=None):
    return ScanRunner(_Runtime(_Rules(findings)), FindingPolicy())


class ScanRunnerTests(unittest.TestCase):
    def test_scanner_findings_get_evidence_and_source_attribution(self):
        scanner = _Scanner("security-agent", [finding(
            rule_id="SCANNED", evidence_refs=[],
        )])
        runner = runner_for([finding(rule_id="LOCAL")])
        findings, _stats, components = runner._scan(
            DIFF, parse_unified_diff(DIFF), ExecutionLedger("r"), [scanner],
        )
        scanned = next(item for item in findings if item.rule_id == "SCANNED")
        self.assertEqual("declarative-scanner", scanned.evidence_refs[0]["tool"])
        self.assertEqual("security-scanner", scanned.evidence_refs[0]["scanner"])
        self.assertEqual("declarative-scanner:security-scanner", scanned.source)
        names = {item["name"] for item in components}
        self.assertIn("local-rule-scanner", names)
        self.assertIn("security-scanner", names)

    def test_scanner_failures_are_recorded_and_skipped(self):
        scanner = _Scanner("broken", error=RuntimeError("scanner failed"))
        runner = runner_for([])
        findings, _stats, _components = runner._scan(
            DIFF, parse_unified_diff(DIFF), ExecutionLedger("r"), [scanner],
        )
        self.assertEqual([], findings)

    def test_high_risk_python_findings_get_ast_evidence(self):
        runner = runner_for([finding(severity=Severity.HIGH)])
        findings, _stats, components = runner._scan(
            DIFF, parse_unified_diff(DIFF), ExecutionLedger("r"), [],
        )
        refs = findings[0].evidence_refs
        self.assertTrue(any(item["tool"] == "diff-ast-analyze" for item in refs))
        self.assertIn("diff-ast-analyze", {item["name"] for item in components})

    def test_ast_evidence_skips_low_severity_and_non_python_findings(self):
        for item, diff in (
            (finding(severity=Severity.MEDIUM), DIFF),
            (finding(severity=Severity.HIGH, path="app.js"),
             "--- a/app.js\n+++ b/app.js\n@@ -0,0 +1 @@\n+eval(raw)\n"),
        ):
            with self.subTest(path=item.path, severity=item.severity):
                runner = runner_for([item])
                findings, _stats, _components = runner._scan(
                    diff, parse_unified_diff(diff), ExecutionLedger("r"), [],
                )
                self.assertFalse(any(
                    ref.get("tool") == "diff-ast-analyze"
                    for ref in findings[0].evidence_refs
                ))

    def test_invalid_python_is_traced_but_not_attached_as_evidence(self):
        diff = "--- a/app.py\n+++ b/app.py\n@@ -0,0 +1 @@\n+def broken(:\n"
        runner = runner_for([finding(severity=Severity.HIGH)])
        ledger = ExecutionLedger("r")
        findings, _stats, _components = runner._scan(
            diff, parse_unified_diff(diff), ledger, [],
        )
        self.assertFalse(any(
            ref.get("tool") == "diff-ast-analyze"
            for ref in findings[0].evidence_refs
        ))
        scan_call = next(
            item for item in ledger.tool_calls if item.tool == "diff-ast-analyze"
        )
        self.assertFalse(scan_call.ok)


if __name__ == "__main__":
    unittest.main()
