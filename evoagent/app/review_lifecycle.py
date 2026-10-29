"""Create, enqueue, resume and cancel review tasks."""
import hashlib
import uuid
from typing import Any, Callable, Dict, Optional

from ..domain.modes import RunMode
from ..domain.models import TaskState, TraceEvent
from ..store_contract import StoreProtocol
from ..time_utils import utc_now
from .policies import ReviewPolicy
from .review_execution import ReviewExecutionService


class ReviewLifecycleService:
    def __init__(
        self,
        store: StoreProtocol,
        queue: Any,
        policy: ReviewPolicy,
        observability: Any,
        releases: Any,
        alerts: Any,
        metrics: Any,
        chat_client_provider: Callable[[], Any],
        execution: ReviewExecutionService,
    ):
        self.store = store
        self.queue = queue
        self.policy = policy
        self.observability = observability
        self.releases = releases
        self.alerts = alerts
        self.metrics = metrics
        self.chat_client_provider = chat_client_provider
        self.execution = execution

    def create_task(
        self, repository: str, diff: str, pull_request: Optional[int], source: str,
        tenant_id: str = "default", repository_root: str = "",
        enabled_agents: Optional[list] = None,
        enabled_skills: Optional[list] = None,
    ) -> str:
        task_id = str(uuid.uuid4())
        encoded = diff.encode("utf-8")
        assignment = self.execution._execution_assignment(tenant_id, task_id)
        self.store.create(task_id, repository, pull_request, {
            "source": source, "diff_bytes": len(encoded),
            "diff_sha256": hashlib.sha256(encoded).hexdigest(),
            **assignment,
            "mode": RunMode.AGENTIC.value,
            "repository_root": repository_root,
            "enabled_agents": enabled_agents or [],
            "enabled_skills": enabled_skills or [],
        }, tenant_id)
        self.store.save_task_payload(task_id, diff)
        return task_id

    def create_deferred_task(
        self, repository: str, pull_request: Optional[int], source: str,
        tenant_id: str, payload: Dict[str, Any], task_id: Optional[str] = None,
    ) -> str:
        task_id = task_id or str(uuid.uuid4())
        assignment = self.execution._execution_assignment(tenant_id, task_id)
        self.store.create(task_id, repository, pull_request, {
            "source": source, "diff_pending": True,
            "repository_root": self.policy.resolve_repository_root(
                repository, "", tenant_id,
            ),
            **assignment,
            **payload,
            "mode": RunMode.AGENTIC.value,
        }, tenant_id)
        return task_id

    def create_review(
        self, repository: str, diff: str, pull_request: Optional[int] = None,
        source: str = "api", tenant_id: str = "default", mode: str = "",
        repository_root: str = "", enabled_agents: Optional[list] = None,
        enabled_skills: Optional[list] = None,
    ) -> Dict[str, Any]:
        self.policy.validate_review(repository, diff)
        RunMode.parse(mode) if mode else None
        self.policy.require_agentic_model(self.chat_client_provider())
        repository_root = self.policy.resolve_repository_root(
            repository, repository_root, tenant_id,
        )
        self.policy.validate_enabled_agents(enabled_agents)
        self.policy.validate_enabled_skills(enabled_skills, tenant_id)
        self.policy.authorize_repository(tenant_id, repository)
        task_id = self.create_task(
            repository, diff, pull_request, source, tenant_id,
            repository_root, enabled_agents, enabled_skills,
        )
        try:
            with self.observability.span(
                "review", task_id, task_id=task_id, tenant_id=tenant_id,
                repository=repository,
            ), self.metrics.timer("review_duration"):
                report = self.execution.run_review(
                    task_id, repository, pull_request, diff, tenant_id,
                )
            self.metrics.inc("reviews_total")
            lane = (self.store.get(task_id, tenant_id).get("input") or {}).get(
                "release_lane", "stable"
            )
            self.releases.observe(tenant_id, "llm-review", False, lane)
            return {
                "task_id": task_id, "state": "SUCCESS",
                "report": report.to_dict(),
            }
        except Exception:
            task = self.store.get(task_id, tenant_id) or {}
            lane = (task.get("input") or {}).get("release_lane", "stable")
            self.releases.observe(tenant_id, "llm-review", True, lane)
            self.alerts.evaluate(tenant_id)
            raise

    def enqueue_review(
        self, repository: str, diff: str, pull_request: Optional[int] = None,
        source: str = "api", github_issue_url: str = "",
        installation_id: Optional[int] = None,
        tenant_id: str = "default", mode: str = "",
        repository_root: str = "", enabled_agents: Optional[list] = None,
        enabled_skills: Optional[list] = None,
    ) -> Dict[str, Any]:
        self.policy.validate_review(repository, diff)
        RunMode.parse(mode) if mode else None
        self.policy.require_agentic_model(self.chat_client_provider())
        repository_root = self.policy.resolve_repository_root(
            repository, repository_root, tenant_id,
        )
        self.policy.validate_enabled_agents(enabled_agents)
        self.policy.validate_enabled_skills(enabled_skills, tenant_id)
        self.policy.authorize_repository(tenant_id, repository)
        task_id = self.create_task(
            repository, diff, pull_request, source, tenant_id,
            repository_root, enabled_agents, enabled_skills,
        )
        self.queue.submit({
            "task_id": task_id, "repository": repository,
            "pull_request": pull_request,
            "github_issue_url": github_issue_url,
            "installation_id": installation_id,
            "tenant_id": tenant_id,
        }, message_id=task_id)
        self.metrics.inc("reviews_enqueued_total")
        return {
            "task_id": task_id, "state": "PENDING",
            "queue": self.queue.backend,
        }

    def resume_task(self, task_id: str, tenant_id: Optional[str] = None) -> dict:
        task = self.store.get(task_id, tenant_id)
        if not task:
            raise ValueError("task not found")
        if task["state"] == "SUCCESS":
            return {"task_id": task_id, "state": "SUCCESS", "report": task["report"]}
        if task["state"] not in {"FAILED", "CANCELLED"}:
            raise ValueError("only failed or cancelled tasks can be resumed")
        diff = self.store.get_task_payload(task_id)
        if diff is None:
            raise ValueError("task payload is no longer available")
        if not self.store.prepare_resume(task_id, task.get("tenant_id", "default")):
            raise ValueError("task state changed; resume was not applied")
        try:
            self.queue.submit({
                "task_id": task_id, "repository": task["repository"],
                "pull_request": task.get("pull_request"),
                "tenant_id": task.get("tenant_id", "default"),
            }, message_id=task_id)
        except Exception as exc:
            step = max([int(item["step"]) for item in task.get("trace", [])] or [0]) + 1
            self.store.fail(task_id, str(exc), TraceEvent(
                step, TaskState.FAILED, "Resume delivery failed: %s" % exc, utc_now(),
            ))
            raise
        return {"task_id": task_id, "state": "PENDING", "resumed": True}

    def cancel_task(self, task_id: str, tenant_id: Optional[str] = None) -> bool:
        return self.store.request_cancel(task_id, tenant_id)
