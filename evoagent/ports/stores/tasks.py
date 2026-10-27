"""Task lifecycle, checkpoints, payloads and cancellation."""
from typing import Any, Dict, Optional, Protocol, runtime_checkable

from ...models import ReviewReport, TraceEvent
from .review import ReviewStore


@runtime_checkable
class TaskStorePort(ReviewStore, Protocol):
    """Task state machine plus the payload/checkpoint data it needs."""

    def create(
        self, task_id: str, repository: str, pull_request: Optional[int],
        payload: Dict[str, Any], tenant_id: str = "default",
    ) -> None:
        ...

    def transition(self, task_id: str, event: TraceEvent) -> None:
        ...

    def succeed(self, task_id: str, report: ReviewReport, event: TraceEvent) -> None:
        ...

    def fail(self, task_id: str, error: str, event: TraceEvent) -> None:
        ...

    def list_tasks(
        self, limit: int = 50, tenant_id: Optional[str] = None,
    ) -> list:
        ...

    def save_task_payload(self, task_id: str, diff: str) -> None:
        ...

    def update_task_input(self, task_id: str, updates: Dict[str, Any]) -> None:
        ...

    def get_task_payload(self, task_id: str) -> Optional[str]:
        ...

    def request_cancel(
        self, task_id: str, tenant_id: Optional[str] = None,
    ) -> bool:
        ...

    def is_cancelled(self, task_id: str) -> bool:
        ...

    def prepare_resume(self, task_id: str, tenant_id: str) -> bool:
        ...

    def cancel(self, task_id: str, event: TraceEvent) -> None:
        ...

    def record_agent_message(
        self, task_id: str, message: Dict[str, Any],
    ) -> None:
        ...
