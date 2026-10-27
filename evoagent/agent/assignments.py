"""Parallel worker assignments and bounded revision rounds."""
import json
from concurrent.futures import ThreadPoolExecutor, as_completed

from ..bounded_role import BoundedRole
from ..prompts import RELIABILITY_PROMPT, ROLE_PERMISSIONS, SECURITY_PROMPT
from ..runtime import ToolRegistry
from .findings import _parse_findings


class AssignmentsMixin:
    def _run_pending_assignments(
        self, task_id, session, diff, parsed, suite, ledger, assignments, revision_round,
        memory_context=None, available_skills=None, max_steps=3, allow_tools=True,
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
            prompt = SECURITY_PROMPT if worker == "security" else (
                RELIABILITY_PROMPT + "\n" + SECURITY_PROMPT.split("Final action:", 1)[-1]
            )
            if self.prompt_overlay:
                prompt += "\nActive validated prompt overlay:\n" + self.prompt_overlay
            if selected_skills:
                prompt += "\n\nActive Agent Skills:\n" + "\n\n".join(
                    "<agent-skill name=\"%s\">\n%s\n</agent-skill>" % (
                        skill.name, skill.instructions,
                    ) for skill in selected_skills
                )
            working_memory_supplier, observation_sink = self._memory_hooks(task_id, worker)
            role = BoundedRole(
                worker, prompt, self.client,
                self._token_budget(worker), self.default_time_budget,
                max_steps=max_steps,
                context_manager=self.context_manager,
                working_memory_supplier=working_memory_supplier,
                observation_sink=observation_sink,
            )
            context = {
                "lead_assignment": assignment,
                "lead_feedback": assignment.get("lead_feedback", ""),
                **self._model_diff(
                    diff, task_id, "%s:assignment" % worker,
                    focus_files=assignment.get("files") or parsed.files,
                    risk_domains=assignment.get("risk_domains") or (),
                ),
                "changed_files": parsed.files,
                "scanner_findings": session.scanner_findings,
                "recalled_memory": memory_context or {"items": []},
                "active_agent_skills": [skill.runtime_entry() for skill in selected_skills],
                "instruction": (
                    (
                        "This is a one-shot review with no tools. Return final findings now; "
                        "do not request a tool. "
                    ) if not allow_tools else ""
                ) + (
                    "Report only to the Lead. Return final findings with exact changed-line "
                    "evidence and address every required_evidence item."
                ),
            }
            tools = (
                suite.registry(
                    worker, self._skill_tool_permissions(worker, selected_skills)
                ) if allow_tools else ToolRegistry()
            )
            if allow_tools:
                self._register_skill_resource_tool(tools, selected_skills)
            return role.run(
                json.dumps(context, ensure_ascii=False),
                tools, ledger,
            )

        with ThreadPoolExecutor(max_workers=max(1, len(pending))) as pool:
            futures = {pool.submit(run, item): item for item in pending}
            for future in as_completed(futures):
                assignment = futures[future]
                run_id = str(assignment.get("run_id") or assignment["assignment_id"])
                try:
                    findings = _parse_findings(
                        future.result(), parsed, assignment["worker"]
                    )
                    result = {
                        "assignment_id": assignment["assignment_id"],
                        "run_id": run_id, "worker": assignment["worker"],
                        "revision_round": revision_round, "status": "completed",
                        "findings": [item.to_dict() for item in findings], "error": "",
                    }
                except Exception as exc:
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
                self._save_lead_session(task_id, session, ledger)
