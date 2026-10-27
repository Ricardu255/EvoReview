"""Failure feedback capture."""
from typing import Any, Optional

from ..store_contract import StoreProtocol


class FeedbackApplicationService:
    def __init__(self, store: StoreProtocol, memory: Any, metrics: Any):
        self.store = store
        self.memory = memory
        self.metrics = metrics

    def record_feedback(
        self, task_id: str, category: str, finding: Optional[dict], note: str,
        tenant_id: Optional[str] = None,
    ) -> dict:
        task = self.store.get(task_id, tenant_id)
        if not task:
            raise ValueError("task not found")
        if task.get("state") != "SUCCESS" or not task.get("report"):
            raise ValueError("feedback requires a completed review task")
        if category not in {"false_positive", "missed_issue", "bad_fix", "accepted"}:
            raise ValueError("unsupported feedback category")
        self.store.record_failure_case(
            task_id, category, {"finding": finding, "note": note[:2000]}
        )
        self.memory.remember_feedback(
            task.get("tenant_id") or tenant_id or "default", task["repository"],
            task_id, category, finding, note[:2000],
        )
        self.metrics.inc("feedback_total")
        return {"recorded": True, "category": category}
