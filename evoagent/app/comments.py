"""GitHub review-comment delivery with bounded retries and manual replay."""
import logging
import time
from typing import Any, Callable, Optional

from ..domain.report import to_markdown


logger = logging.getLogger("evoagent")


class ReviewCommentService:
    """Deliver review comments independently from review success.

    GitHub availability must not fail (and re-run) an otherwise successful
    review, but a transient outage should not lose the comment either.  The
    task-scoped HTML marker makes re-delivery idempotent.
    """

    ATTEMPTS = 3
    BACKOFF_SECONDS = 1.0

    def __init__(
        self,
        settings: Any,
        store: Any,
        metrics: Any,
        github_client_for_installation: Callable[[Optional[int]], Any],
    ):
        self.settings = settings
        self.store = store
        self.metrics = metrics
        self.github_client_for_installation = github_client_for_installation

    def deliver(
        self, issue_url: str, installation_id: Optional[int], task_id: str,
        tenant_id: str, report: Any,
    ) -> bool:
        value = report.to_dict() if hasattr(report, "to_dict") else report
        error = None
        for attempt in range(1, self.ATTEMPTS + 1):
            try:
                client = self.github_client_for_installation(installation_id)
                client.upsert_comment(
                    issue_url,
                    to_markdown(value, self.settings.report_max_findings),
                    "<!-- evoagent-review:%s -->" % task_id,
                )
                self.metrics.inc("comment_delivered_total")
                return True
            except Exception as exc:
                error = exc
                if attempt < self.ATTEMPTS:
                    time.sleep(self.BACKOFF_SECONDS * (2 ** (attempt - 1)))
        logger.warning(
            "failed to deliver review comment for task %s after %d attempts",
            task_id, self.ATTEMPTS, exc_info=error,
        )
        self.metrics.inc("comment_delivery_failed_total")
        try:
            self.store.audit(
                tenant_id, "service", "comment.delivery_failed", task_id,
                {"issue_url": issue_url, "error": str(error)[:500]},
            )
        except Exception:
            # Delivery failure must never turn a successful review into a
            # failed/retried task, even if the audit store is unavailable.
            logger.exception("failed to audit comment delivery failure")
        return False

    def redeliver(self, task_id: str, tenant_id: Optional[str] = None) -> dict:
        """Re-post a stored review comment after a transient GitHub outage."""
        task = self.store.get(task_id, tenant_id)
        if not task:
            raise ValueError("task not found")
        if task.get("state") != "SUCCESS" or not task.get("report"):
            raise ValueError("only successful tasks with a report can be re-posted")
        task_input = task.get("input") or {}
        issue_url = task_input.get("github_issue_url", "")
        if not issue_url:
            raise ValueError("task has no GitHub comment target")
        delivered = self.deliver(
            issue_url, task_input.get("installation_id"), task_id,
            task.get("tenant_id") or tenant_id or "default", task["report"],
        )
        return {"task_id": task_id, "delivered": delivered}
