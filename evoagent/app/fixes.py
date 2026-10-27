"""Automatic repair delivery."""
from typing import Any, Callable, Optional

from ..store_contract import StoreProtocol


class FixesApplicationService:
    def __init__(
        self,
        store: StoreProtocol,
        fixer: Any,
        github_client_for_installation: Callable[[Optional[int]], Any],
        metrics: Any,
    ):
        self.store = store
        self.fixer = fixer
        self.github_client_for_installation = github_client_for_installation
        self.metrics = metrics

    def create_fix(
        self, task_id: str, installation_id: Optional[int] = None,
        tenant_id: Optional[str] = None,
    ) -> dict:
        task = self.store.get(task_id, tenant_id)
        if not task or not task.get("report"):
            raise ValueError("completed task not found")
        if task.get("pull_request") is None:
            raise ValueError("fix commits require a GitHub pull request task")
        actual_tenant = task.get("tenant_id") or tenant_id or "default"
        if not self.store.repository_allowed(actual_tenant, task["repository"], True):
            raise PermissionError("automatic repair is not enabled for this repository")
        result = self.fixer.create_fix_commits(
            self.github_client_for_installation(installation_id),
            task["repository"], task["pull_request"], task["report"],
        )
        self.metrics.inc("fix_runs_total")
        return result
