"""Parallel worker assignments and bounded revision rounds."""
from .collaborators import RuntimeBackref
import json
from concurrent.futures import ThreadPoolExecutor, as_completed

from .bounded_role import BoundedRole
from ..prompts import RELIABILITY_PROMPT, SECURITY_PROMPT
from .runtime import ToolRegistry
from .findings import _parse_findings
from ..domain.finding_identity import (
    extract_declared_rule_ids, worker_rule_catalog,
)
from .tools import READ_ONLY_TOOL_NAMES


class AssignmentRunner(RuntimeBackref):
    def __init__(self, runtime, memory_hooks, tools, sessions):
        super().__init__(runtime)
        self.memory_hooks = memory_hooks
        self.tools = tools
        self.sessions = sessions

    def _run_pending_assignments(
        self, task_id, session, diff, parsed, suite, ledger, assignments, revision_round,
        memory_context=None, available_skills=None, max_steps=3, allow_tools=True,
        tool_scope="full",
    ):
        pending = [
            item for item in assignments
            if str(item.get("run_id") or item["assignment_id"])
            not in session.worker_results
        ]
        if not pending:
            return

        def run(assignment):
            worker = assignment["worker"]
            selected_skills = [
                available_skills[name]
                for name in assignment.get("skills") or []
                if name in (available_skills or {})
            ]
            custom_rule_ids = sorted(extract_declared_rule_ids(
                skill.instructions for skill in selected_skills
            ))
            prompt = SECURITY_PROMPT if worker == "security" else (
                RELIABILITY_PROMPT + "\n" + SECURITY_PROMPT.split("Final action:", 1)[-1]
            )
            if self.prompt_overlay:
                prompt += "\nActive validated prompt overlay:\n" + self.prompt_overlay
            # In lossless (batched) mode the skill instructions travel inside
            # the metered task context instead of the prompt, keeping the
            # pre-charged request within the role budget.
            if selected_skills and not getattr(self.client, "lossless", False):
                prompt += "\n\nActive Agent Skills:\n" + "\n\n".join(
                    "<agent-skill name=\"%s\">\n%s\n</agent-skill>" % (
                        skill.name, skill.instructions,
                    ) for skill in selected_skills
                )
            working_memory_supplier, observation_sink = self.memory_hooks._memory_hooks(task_id, worker)
            role = BoundedRole(
                worker, prompt, self.client,
                self.runtime.token_budget(worker), self.default_time_budget,
                max_steps=max_steps,
                context_manager=self.context_manager,
                working_memory_supplier=working_memory_supplier,
                observation_sink=observation_sink,
            )
            context = {
                "lead_assignment": assignment,
                "lead_feedback": assignment.get("lead_feedback", ""),
                **self.runtime.model_diff(
                    diff, task_id, "%s:assignment" % worker,
                    focus_files=assignment.get("files") or parsed.files,
                    risk_domains=assignment.get("risk_domains") or (),
                ),
                "changed_files": parsed.files,
                "scanner_findings": session.scanner_findings,
                "recalled_memory": memory_context or {"items": []},
                "active_agent_skills": [skill.runtime_entry() for skill in selected_skills],
                "finding_rule_catalog": worker_rule_catalog(worker),
                "custom_skill_rule_ids": custom_rule_ids,
                "instruction": (
                    (
                        "This is a one-shot review with no tools. Return final findings now; "
                        "do not request a tool. "
                    ) if not allow_tools else ""
                ) + (
                    "Report only to the Lead. Choose rule_id only from finding_rule_catalog "
                    "or custom_skill_rule_ids; if none fits, use UNCLASSIFIED-SECURITY or "
                    "UNCLASSIFIED-CORRECTNESS as appropriate. Do not infer CWE. Return final "
                    "findings with exact changed-line evidence and address every "
                    "required_evidence item."
                ) + (
                    " Review additions and removals. For deleted evidence, use line_side='old', "
                    "the old path and old line number."
                    if getattr(self.client, "lossless", False) else ""
                ),
            }
            tools = (
                suite.registry(
                    worker,
                    (
                        self.tools._skill_tool_permissions(worker, selected_skills)
                        & READ_ONLY_TOOL_NAMES
                    ) if tool_scope == "read-only" else
                    self.tools._skill_tool_permissions(worker, selected_skills),
                ) if allow_tools else ToolRegistry()
            )
            if allow_tools:
                self.tools._register_skill_resource_tool(tools, selected_skills)
            return {
                "role_result": role.run(
                    json.dumps(context, ensure_ascii=False), tools, ledger,
                ),
                "custom_rule_ids": custom_rule_ids,
            }

        lossless = getattr(self.client, "lossless", False)
        failure = None
        # Metered budget accounting is per ledger; serial workers keep the
        # reservation order deterministic in batched mode.
        with ThreadPoolExecutor(max_workers=1 if lossless else max(1, len(pending))) as pool:
            futures = {pool.submit(run, item): item for item in pending}
            for future in as_completed(futures):
                assignment = futures[future]
                run_id = str(assignment.get("run_id") or assignment["assignment_id"])
                try:
                    completed = future.result()
                    findings = _parse_findings(
                        completed["role_result"], parsed, assignment["worker"],
                        completed["custom_rule_ids"],
                    )
                    result = {
                        "assignment_id": assignment["assignment_id"],
                        "run_id": run_id, "worker": assignment["worker"],
                        "revision_round": revision_round, "status": "completed",
                        "findings": [item.to_dict() for item in findings], "error": "",
                    }
                except Exception as exc:
                    if lossless:
                        # A metered worker failure means its budget is already
                        # charged; fail fast instead of burning the remaining
                        # budget on siblings heading for the same outage.
                        self.sessions._save_lead_session(task_id, session, ledger)
                        failure = failure or exc
                        for waiting in futures:
                            waiting.cancel()
                        continue
                    result = {
                        "assignment_id": assignment["assignment_id"],
                        "run_id": run_id, "worker": assignment["worker"],
                        "revision_round": revision_round, "status": "failed",
                        "findings": [], "error": str(exc)[:1000],
                    }
                session.worker_results[run_id] = result
                ledger.trace(
                    "lead-session", "worker_reported",
                    assignment_id=assignment["assignment_id"], run_id=run_id,
                    worker=assignment["worker"], status=result["status"],
                    findings=len(result["findings"]), revision_round=revision_round,
                )
                self.sessions._save_lead_session(task_id, session, ledger)
        if failure:
            raise failure
