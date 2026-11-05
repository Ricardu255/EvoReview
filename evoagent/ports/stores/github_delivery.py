"""Cross-task GitHub delivery state, keyed by PR and head SHA."""
from typing import Any, Dict, Optional, Protocol, runtime_checkable


@runtime_checkable
class GitHubDeliveryStorePort(Protocol):
    def get_github_delivery_state(
        self, tenant_id: str, repository: str, pull_request: int, head_sha: str,
    ) -> Optional[Dict[str, Any]]:
        ...

    def mark_github_inline_delivered(
        self, tenant_id: str, repository: str, pull_request: int, head_sha: str,
    ) -> None:
        ...

    def set_github_check_run(
        self, tenant_id: str, repository: str, pull_request: int,
        head_sha: str, check_run_id: int,
    ) -> None:
        ...
