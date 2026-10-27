"""High-level Lead/Worker/Critic orchestration."""
from ..lead_session import LeadSession
from ..models import ComponentKind
from ..modes import component
from ..prompts import ROLE_PERMISSIONS


class OrchestrationMixin:
    def _agentic(
        self, task_id, diff, parsed, suite, ledger, enabled, scanners=None,
        memory_context=None, available_skills=None, requested_skills=None,
    ):
        if "lead" not in enabled:
            raise ValueError("agentic mode requires the lead Agent")
        worker_roles = [
            name for name in ("security", "correctness-reliability")
            if name in enabled
        ]
        session = LeadSession.load(self.store, task_id, ledger, self.context_manager)
        memory_context = memory_context or {
            "trust": "untrusted historical hints; verify with current diff or tools",
            "items": [],
        }
        available_skills = dict(available_skills or {})
        requested_skills = list(requested_skills or [])

        if not session.scanner_complete:
            rule_findings, _, scanner_components = self._scan(
                diff, parsed, ledger, scanners,
            )
            session.scanner_findings = [item.to_dict() for item in rule_findings]
            session.scanner_components = scanner_components
            session.scanner_complete = True
            session.phase = "scanned"
            self._save_lead_session(task_id, session, ledger)
        rule_findings = self._restore_findings(session.scanner_findings)

        if not session.delegations:
            decision = self._run_lead(
                "delegate", {
                    **self._model_diff(
                        diff, task_id, "lead:delegate", focus_files=parsed.files,
                    ),
                    "changed_files": parsed.files,
                    "enabled_workers": worker_roles,
                    "available_agent_skills": [
                        available_skills[name].catalog_entry()
                        for name in sorted(available_skills)
                    ],
                    "requested_agent_skills": requested_skills,
                    "scanner_findings": session.scanner_findings,
                    "recalled_memory": memory_context,
                }, suite, ledger, task_id, max_steps=1, allow_tools=False,
            )
            session.delegations = LeadSession.normalize_delegations(
                decision.get("delegations"), worker_roles, parsed.files,
                set(available_skills), requested_skills,
            )
            session.lead_delegation = self._public_decision(decision)
            session.risk_level = LeadSession.normalize_risk_level(
                decision.get("risk_level")
            )
            session.phase = "delegated"
            for assignment in session.delegations:
                ledger.trace(
                    "lead-session", "assignment_created",
                    assignment_id=assignment["assignment_id"],
                    worker=assignment["worker"],
                    objective=assignment["objective"][:500],
                )
            self._save_lead_session(task_id, session, ledger)

        risk_level = session.risk_level
        high_risk = risk_level == "high"
        self._run_pending_assignments(
            task_id, session, diff, parsed, suite, ledger,
            session.delegations, revision_round=0,
            memory_context=memory_context,
            available_skills=available_skills,
            max_steps=3 if high_risk else 1,
            allow_tools=high_risk,
        )
        session.phase = "workers-completed"
        self._save_lead_session(task_id, session, ledger)

        final_assessment = {}
        if high_risk:
            candidates = self._session_candidates(session)
            if not session.lead_assessments:
                assessment = self._run_lead(
                    "assess-workers", {
                        **self._model_diff(
                            diff, task_id, "lead:assess-workers",
                            focus_files=[item.path for item in candidates],
                        ),
                        "assignments": session.delegations,
                        "worker_results": list(session.worker_results.values()),
                        "candidate_findings": [item.to_dict() for item in candidates],
                        "revision_round": 0,
                        "remaining_revision_rounds": 1,
                        "recalled_memory": memory_context,
                    }, suite, ledger, task_id, max_steps=1, allow_tools=False,
                )
                session.lead_assessments.append(self._public_decision(assessment))
                self._save_lead_session(task_id, session, ledger)
            else:
                assessment = session.lead_assessments[0]
            final_assessment = assessment
            requests = LeadSession.normalize_revision_requests(
                assessment.get("revision_requests"), session.delegations,
            )
            revision_assignments = []
            for request in requests:
                key = "1:%s" % request["assignment_id"]
                if key in session.revision_results:
                    continue
                original = next(
                    item for item in session.delegations
                    if item["assignment_id"] == request["assignment_id"]
                )
                revision = dict(original)
                revision["run_id"] = key
                revision["revision_round"] = 1
                revision["lead_feedback"] = request["guidance"]
                revision["required_evidence"] = request["required_evidence"]
                revision_assignments.append(revision)
            self._run_pending_assignments(
                task_id, session, diff, parsed, suite, ledger, revision_assignments,
                revision_round=1,
                memory_context=memory_context,
                available_skills=available_skills,
                max_steps=2,
                allow_tools=True,
            )
            for revision in revision_assignments:
                key = revision["run_id"]
                result = session.worker_results.pop(key)
                session.revision_results[key] = result
                session.worker_results[revision["assignment_id"]] = result
                ledger.trace(
                    "lead-session", "revision_completed",
                    assignment_id=revision["assignment_id"],
                    worker=revision["worker"], round=1,
                    status=result["status"],
                )
            if requests:
                session.revision_rounds = 1
                session.phase = "revision-1-completed"
                self._save_lead_session(task_id, session, ledger)

        candidates = self._session_candidates(session)
        session.candidate_findings_before_critic = len(candidates)
        if "critic" in enabled and not session.critic_complete:
            critic_result = self._run_critic(
                diff, candidates,
                str(final_assessment.get("critic_objective", "")),
                suite, ledger, task_id, memory_context,
                max_steps=1, allow_tools=False,
            )
            candidates, decisions = self._apply_critic(critic_result, candidates)
            session.critic_decisions = decisions
            session.critic_candidates = [item.to_dict() for item in candidates]
            session.critic_complete = True
            session.phase = "critic-completed"
            self._save_lead_session(task_id, session, ledger)
        elif session.critic_complete:
            candidates = self._restore_findings(session.critic_candidates or [])
        else:
            session.critic_decisions = [
                {"finding_index": index, "accepted": True, "objections": []}
                for index in range(len(candidates))
            ]
            session.critic_candidates = [item.to_dict() for item in candidates]
            session.critic_complete = True

        if not session.lead_final:
            final_decision = self._run_lead(
                "finalize", {
                    **self._model_diff(
                        diff, task_id, "lead:finalize",
                        focus_files=[item.path for item in candidates],
                    ),
                    "candidate_findings": [
                        {"finding_index": index, **item.to_dict()}
                        for index, item in enumerate(candidates)
                    ],
                    "critic_decisions": session.critic_decisions,
                    "worker_results": list(session.worker_results.values()),
                    "instruction": (
                        "Return the indices that should be published. Resolve critic objections "
                        "explicitly and prefer changed-line tool evidence."
                    ),
                    "recalled_memory": memory_context,
                }, suite, ledger, task_id, max_steps=1, allow_tools=False,
            )
            if "accepted_finding_indices" not in final_decision:
                final_decision["accepted_finding_indices"] = [
                    int(item["finding_index"])
                    for item in session.critic_decisions
                    if item.get("accepted")
                ]
            session.lead_final = self._public_decision(final_decision)
        accepted = self._apply_lead_final(session.lead_final, candidates)
        session.accepted_findings = [item.to_dict() for item in accepted]
        session.phase = "completed"
        session.stop_reason = (
            "high-risk-one-revision-round" if session.revision_rounds
            else "high-risk-single-pass" if high_risk else "single-pass"
        )
        self._save_lead_session(task_id, session, ledger, completed=True)

        roles = [
            name for name in ("lead", "security", "correctness-reliability", "critic")
            if name in enabled
        ]
        collaboration = {
            "protocol": "lead-workers",
            "roles": roles,
            "risk_level": risk_level,
            "revision_rounds": int(session.revision_rounds),
            "lead": {
                "delegation": session.lead_delegation or {},
                "assessments": session.lead_assessments,
                "final": session.lead_final,
            },
            "assignments": session.delegations,
            "agent_skills": sorted({
                name for assignment in session.delegations
                for name in assignment.get("skills") or []
            }),
            "worker_results": list(session.worker_results.values()),
            "revision_results": list(session.revision_results.values()),
            "scanner_findings": len(rule_findings),
            "candidate_findings_before_critic": session.candidate_findings_before_critic,
            "accepted_findings": len(accepted),
            "critic_decisions": session.critic_decisions,
            "stop_reason": session.stop_reason,
        }
        components = session.scanner_components + [
            component(
                ComponentKind.LLM_AGENT, name,
                token_budget=self._token_budget(name),
                time_budget_seconds=self.default_time_budget,
                tool_permissions=(
                    sorted(ROLE_PERMISSIONS[name])
                    if high_risk and name in {
                        "security", "correctness-reliability",
                    } else []
                ),
            )
            for name in roles
        ]
        return accepted, collaboration, components
