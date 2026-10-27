"""Lead and Critic role loops."""
from .collaborators import RuntimeBackref
import json

from ..bounded_role import BoundedRole
from ..prompts import CRITIC_PROMPT, LEAD_PROMPT, ROLE_PERMISSIONS
from ..runtime import ToolRegistry


class LeadRunner(RuntimeBackref):
    def __init__(self, runtime, memory_hooks):
        super().__init__(runtime)
        self.memory_hooks = memory_hooks

    def _run_lead(
        self, phase, payload, suite, ledger, context_key="",
        max_steps=1, allow_tools=False,
    ):
        working_memory_supplier, observation_sink = self.memory_hooks._memory_hooks(context_key, "lead")
        role = BoundedRole(
            "lead", LEAD_PROMPT + (
                ("\nActive validated prompt overlay:\n" + self.prompt_overlay)
                if self.prompt_overlay else ""
            ), self.client, self.runtime.token_budget("lead"), self.default_time_budget,
            max_steps=max_steps,
            context_manager=self.context_manager,
            working_memory_supplier=working_memory_supplier,
            observation_sink=observation_sink,
        )
        context = {"phase": phase, **payload}
        if not allow_tools:
            context["execution_policy"] = (
                "This is a one-shot phase with no tools. Return the required final JSON now; "
                "do not request a tool."
            )
        ledger.trace("lead-session", "lead_activated", phase=phase)
        result = role.run(
            json.dumps(context, ensure_ascii=False),
            (
                suite.registry("lead", ROLE_PERMISSIONS["lead"])
                if allow_tools else ToolRegistry()
            ),
            ledger,
        )
        ledger.trace("lead-session", "lead_completed", phase=phase)
        return result

    def _run_critic(
        self, diff, candidates, objective, suite, ledger, context_key="",
        memory_context=None, max_steps=1, allow_tools=False,
    ):
        blinded = [
            {
                "finding_index": index, "rule_id": item.rule_id,
                "severity": item.severity.value, "title": item.title,
                "explanation": item.explanation, "path": item.path,
                "line": item.line, "evidence": item.evidence,
                "evidence_refs": item.evidence_refs, "call_chain": item.call_chain,
                "fix": item.fix, "test": item.test, "confidence": item.confidence,
            }
            for index, item in enumerate(candidates)
        ]
        working_memory_supplier, observation_sink = self.memory_hooks._memory_hooks(context_key, "critic")
        role = BoundedRole(
            "critic", CRITIC_PROMPT + (
                ("\nActive validated prompt overlay:\n" + self.prompt_overlay)
                if self.prompt_overlay else ""
            ), self.client, self.runtime.token_budget("critic"), self.default_time_budget,
            max_steps=max_steps,
            context_manager=self.context_manager,
            working_memory_supplier=working_memory_supplier,
            observation_sink=observation_sink,
        )
        return role.run(
            json.dumps({
                "lead_assignment": objective or (
                    "Blindly challenge every candidate and report explicit decisions."
                ),
                **self.runtime.model_diff(
                    diff, context_key, "critic:blind-review",
                    focus_files=[item.path for item in candidates],
                ),
                "candidates": blinded,
                "recalled_memory": memory_context or {"items": []},
                "instruction": (
                    "Perform one evidence-based pass and return final decisions now. "
                    "Do not request tools."
                ) if not allow_tools else "Use tools only when essential, then finish.",
            }, ensure_ascii=False),
            (
                suite.registry("critic", ROLE_PERMISSIONS["critic"])
                if allow_tools else ToolRegistry()
            ),
            ledger,
        )
