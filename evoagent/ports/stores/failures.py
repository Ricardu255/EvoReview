"""Failure cases and labelled evaluation cases."""
from typing import Any, Dict, Optional, Protocol, runtime_checkable


@runtime_checkable
class FailureStorePort(Protocol):
    def record_failure_case(
        self, task_id: str, category: str, payload: Dict[str, Any],
    ) -> None:
        ...

    def list_failure_cases(
        self, unresolved_only: bool = False, limit: int = 100,
        tenant_id: Optional[str] = None,
    ) -> list:
        ...

    def list_task_failure_cases(
        self, task_id: str, tenant_id: Optional[str] = None,
    ) -> list:
        ...

    def resolve_failure_cases(self, case_ids: list) -> None:
        ...

    def save_evaluation_case(
        self, name: str, split: str, diff: str, expected: list,
        source: str = "manual", active: bool = True, tenant_id: str = "default",
    ) -> Dict[str, Any]:
        ...

    def list_evaluation_cases(
        self, split: Optional[str] = None, active_only: bool = True,
        limit: int = 100, tenant_id: str = "default",
    ) -> list:
        ...
