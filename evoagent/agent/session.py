"""LeadSession load/save helpers."""
from .collaborators import ReviewerBackref
from ..lead_session import LeadSession


class SessionManager(ReviewerBackref):
    def _session_candidates(self, session):
        findings = self._restore_findings(session.scanner_findings)
        for result in session.worker_results.values():
            findings.extend(self._restore_findings(result.get("findings") or []))
        return self._merge(findings)

    def _load_lead_session(self, task_id, ledger):
        return LeadSession.load(
            self.store, task_id, ledger, self.context_manager,
        )

    def _save_lead_session(self, task_id, session, ledger, completed=False):
        session.save(
            self.store, task_id, ledger,
            self.context_manager.summary(task_id), completed,
        )
