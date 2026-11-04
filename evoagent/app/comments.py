"""GitHub review-comment delivery with bounded retries and manual replay."""
import hashlib
import logging
import time
from typing import Any, Callable, Dict, Optional

from ..domain.report import to_inline_markdown, to_markdown


logger = logging.getLogger("evoagent")


def review_comment_marker(tenant_id: str, issue_url: str, task_id: str) -> str:
    """Return a stable marker for one PR, not for one delivery.

    A new delivery (for example ``synchronize``) creates a new task, but it
    should update the same PR comment instead of adding another one.  The
    tenant is part of the key so two tenants reviewing the same upstream PR do
    not overwrite each other's comment.
    """
    if not issue_url:
        return "<!-- evoagent-review:%s -->" % task_id
    key = hashlib.sha256(
        ("%s\0%s" % (tenant_id or "default", issue_url.rstrip("/"))).encode("utf-8")
    ).hexdigest()[:24]
    return "<!-- evoagent-review:%s -->" % key


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
        tenant_id: str, report: Any, repository: str = "",
        pull_request: Optional[int] = None, head_sha: str = "",
    ) -> bool:
        value = report.to_dict() if hasattr(report, "to_dict") else report
        error = None
        for attempt in range(1, self.ATTEMPTS + 1):
            try:
                client = self.github_client_for_installation(installation_id)
                client.upsert_comment(
                    issue_url,
                    to_markdown(value, self.settings.report_max_findings),
                    review_comment_marker(tenant_id, issue_url, task_id),
                )
                self.metrics.inc("comment_delivered_total")
                self._deliver_inline(
                    client, task_id, tenant_id, repository, pull_request, value,
                )
                self._deliver_check_run(
                    client, task_id, tenant_id, repository, head_sha, value,
                )
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

    def _deliver_inline(
        self, client: Any, task_id: str, tenant_id: str, repository: str,
        pull_request: Optional[int], value: Dict[str, Any],
    ) -> None:
        if not getattr(self.settings, "inline_review_comments", False):
            return
        if not repository or pull_request is None:
            return
        try:
            comments = []
            for finding in value.get("findings", [])[:20]:
                if not finding.get("path") or not finding.get("line"):
                    continue
                comments.append({
                    "path": str(finding["path"]),
                    "line": int(finding["line"]),
                    "side": "RIGHT",
                    "body": to_inline_markdown(finding),
                })
            if not comments:
                return
            client.post_review(
                repository, int(pull_request),
                "EvoReview found %d actionable issue(s) in this change."
                % len(comments),
                comments,
            )
            self.metrics.inc("inline_comments_delivered_total", len(comments))
        except Exception as exc:
            # The summary comment is already delivered.  Inline comments are a
            # best-effort enhancement and must not trigger a full redelivery.
            logger.warning(
                "failed to deliver inline review for task %s: %s", task_id, exc,
            )
            self.metrics.inc("inline_comments_failed_total")
            try:
                self.store.audit(
                    tenant_id, "service", "comment.inline_failed", task_id,
                    {"repository": repository, "pull_request": pull_request,
                     "error": str(exc)[:500]},
                )
            except Exception:
                logger.exception("failed to audit inline comment failure")

    def _deliver_check_run(
        self, client: Any, task_id: str, tenant_id: str, repository: str,
        head_sha: str, value: Dict[str, Any],
    ) -> None:
        if not getattr(self.settings, "auto_post_checks", False):
            return
        if not repository or not head_sha:
            return
        findings = value.get("findings", [])
        severities = {
            str(item.get("severity", "")).lower() for item in findings
        }
        if severities.intersection({"critical", "high"}):
            conclusion = "failure"
        elif findings:
            conclusion = "neutral"
        else:
            conclusion = "success"
        title = "EvoReview: %d finding(s), risk %s" % (
            len(findings), value.get("risk", "unknown"),
        )
        summary = str(value.get("summary", ""))[:65000]
        try:
            client.create_check_run(
                repository, head_sha, conclusion, title, summary,
            )
            self.metrics.inc("check_runs_delivered_total")
        except Exception as exc:
            logger.warning(
                "failed to deliver check run for task %s: %s", task_id, exc,
            )
            self.metrics.inc("check_runs_failed_total")
            try:
                self.store.audit(
                    tenant_id, "service", "check_run.failed", task_id,
                    {"repository": repository, "head_sha": head_sha,
                     "error": str(exc)[:500]},
                )
            except Exception:
                logger.exception("failed to audit check-run failure")

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
            task.get("repository", ""), task.get("pull_request"),
            task_input.get("head_sha", ""),
        )
        return {"task_id": task_id, "delivered": delivered}
