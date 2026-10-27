"""LeadSession load/save helpers."""
from .collaborators import RuntimeBackref
from ..lead_session import LeadSession


class SessionManager(RuntimeBackref):
    def __init__(self, runtime, findings):
        super().__init__(runtime)
        self.findings = findings

    def _session_candidates(self, session):
        findings = self.findings._restore_findings(session.scanner_findings)
        for result in session.worker_results.values():
            findings.extend(
                self.findings._restore_findings(result.get("findings") or [])
            )
        return self.findings._merge(findings)

    def _load_lead_session(self, task_id, ledger):
        return LeadSession.load(
            self.store, task_id, ledger, self.context_manager,
        )

    def _save_lead_session(self, task_id, session, ledger, completed=False):
        session.save(
            self.store, task_id, ledger,
            self.context_manager.summary(task_id), completed,
        )
