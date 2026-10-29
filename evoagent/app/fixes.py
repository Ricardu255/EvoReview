"""Automatic repair delivery."""
from typing import Any, Callable, Optional

from ..ports.stores import StoreProtocol


class FixesApplicationService:
    def __init__(
        self,
        store: StoreProtocol,
        fixer: Any,
        github_client_for_installation: Callable[[Optional[int]], Any],
        metrics: Any,
        policy: Any = None,
    ):
        self.store = store
        self.fixer = fixer
        self.github_client_for_installation = github_client_for_installation
        self.metrics = metrics
        self.policy = policy

    def _authorize(self, tenant_id: str, repository: str) -> None:
        if self.policy is not None:
            self.policy.authorize_auto_fix(tenant_id, repository)
            return
        # Compatibility for callers that construct this service without a
        # policy object: keep the store-level check.
        if not self.store.repository_allowed(tenant_id, repository, True):
            raise PermissionError(
                "automatic repair is not enabled for this repository"
            )

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
        self._authorize(actual_tenant, task["repository"])
        task_input = task.get("input") or {}
        bound_installation = task_input.get("installation_id")
        resolved_installation = (
            installation_id if installation_id is not None else bound_installation
        )
        if resolved_installation is not None:
            try:
                resolved_installation = int(resolved_installation)
            except (TypeError, ValueError):
                raise ValueError("installation_id must be an integer")
            owner = self.store.installation_tenant(resolved_installation)
            # Unknown installations are not implicitly owned by the default
            # tenant; an operator must bind them through /github/setup first.
            if owner != actual_tenant:
                raise PermissionError(
                    "GitHub installation is not authorized for this tenant"
                )
        client = self.github_client_for_installation(resolved_installation)
        ensure_access = getattr(client, "ensure_repository_access", None)
        if callable(ensure_access):
            ensure_access(task["repository"])
        result = self.fixer.create_fix_commits(
            client, task["repository"], task["pull_request"], task["report"],
        )
        self.metrics.inc("fix_runs_total")
        return result
