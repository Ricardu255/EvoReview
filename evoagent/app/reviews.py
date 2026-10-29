"""Compatibility facade for review lifecycle and execution services."""
from typing import Any, Callable, Dict, Optional

from ..config import Settings
from ..ports.stores import StoreProtocol
from .policies import ReviewPolicy
from .review_execution import ReviewExecutionService, mark_dead_letter
from .review_lifecycle import ReviewLifecycleService

__all__ = ["ReviewsApplicationService", "mark_dead_letter"]


class ReviewsApplicationService:
    def __init__(
        self,
        settings: Settings,
        store: StoreProtocol,
        queue: Any,
        policy: ReviewPolicy,
        observability: Any,
        releases: Any,
        alerts: Any,
        metrics: Any,
        build_agentic_reviewer: Callable[..., Any],
        harness_factory: Callable[[Any], Any],
        github_client_for_installation: Callable[[Optional[int]], Any],
        chat_client_provider: Callable[[], Any],
    ):
        self.settings = settings
        self.store = store
        self.queue = queue
        self.policy = policy
        self.execution = ReviewExecutionService(
            settings, store, policy, observability, releases, alerts, metrics,
            build_agentic_reviewer, harness_factory, github_client_for_installation,
        )
        self.lifecycle = ReviewLifecycleService(
            store, queue, policy, observability, releases, alerts, metrics,
            chat_client_provider, self.execution,
        )

    def _execution_assignment(self, tenant_id: str, task_id: str) -> dict:
        return self.execution._execution_assignment(tenant_id, task_id)

    def _release_result(self, report) -> dict:
        return self.execution._release_result(report)

    def _run_shadow(
        self, task_id, repository, pull_request, diff, tenant_id, task_input, primary,
    ):
        return self.execution._run_shadow(
            task_id, repository, pull_request, diff, tenant_id, task_input, primary,
        )

    def run_review(
        self, task_id: str, repository: str, pull_request: Optional[int],
        diff: str, tenant_id: str,
    ):
        return self.execution.run_review(
            task_id, repository, pull_request, diff, tenant_id,
        )

    def create_task(
        self, repository: str, diff: str, pull_request: Optional[int], source: str,
        tenant_id: str = "default", repository_root: str = "",
        enabled_agents: Optional[list] = None,
        enabled_skills: Optional[list] = None,
    ) -> str:
        return self.lifecycle.create_task(
            repository, diff, pull_request, source, tenant_id,
            repository_root, enabled_agents, enabled_skills,
        )

    def create_deferred_task(
        self, repository: str, pull_request: Optional[int], source: str,
        tenant_id: str, payload: Dict[str, Any], task_id: Optional[str] = None,
    ) -> str:
        return self.lifecycle.create_deferred_task(
            repository, pull_request, source, tenant_id, payload, task_id,
        )

    def create_review(
        self, repository: str, diff: str, pull_request: Optional[int] = None,
        source: str = "api", tenant_id: str = "default", mode: str = "",
        repository_root: str = "", enabled_agents: Optional[list] = None,
        enabled_skills: Optional[list] = None,
    ) -> Dict[str, Any]:
        return self.lifecycle.create_review(
            repository, diff, pull_request, source, tenant_id, mode,
            repository_root, enabled_agents, enabled_skills,
        )

    def enqueue_review(
        self, repository: str, diff: str, pull_request: Optional[int] = None,
        source: str = "api", github_issue_url: str = "",
        installation_id: Optional[int] = None,
        tenant_id: str = "default", mode: str = "",
        repository_root: str = "", enabled_agents: Optional[list] = None,
        enabled_skills: Optional[list] = None,
    ) -> Dict[str, Any]:
        return self.lifecycle.enqueue_review(
            repository, diff, pull_request, source, github_issue_url,
            installation_id, tenant_id, mode, repository_root,
            enabled_agents, enabled_skills,
        )

    def process_queued(self, payload: Dict[str, Any]) -> None:
        return self.execution.process_queued(payload)

    def on_dead_letter(self, payload: Dict[str, Any], error: str) -> None:
        return self.execution.on_dead_letter(payload, error)

    def resume_task(self, task_id: str, tenant_id: Optional[str] = None) -> dict:
        return self.lifecycle.resume_task(task_id, tenant_id)

    def cancel_task(self, task_id: str, tenant_id: Optional[str] = None) -> bool:
        return self.lifecycle.cancel_task(task_id, tenant_id)
