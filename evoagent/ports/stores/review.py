"""Narrow persistence surface used by the agentic review engine."""
from typing import Any, Dict, Optional, Protocol, runtime_checkable


@runtime_checkable
class ReviewStore(Protocol):
    """Store operations required to run and resume one review task."""

    def get(
        self, task_id: str, tenant_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        ...

    def save_checkpoint(
        self, task_id: str, node: str, state: Dict[str, Any],
        status: str = "completed", attempt: int = 1, error: str = "",
    ) -> None:
        ...

    def load_checkpoints(self, task_id: str) -> Dict[str, Dict[str, Any]]:
        ...
