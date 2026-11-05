"""Periodic retention cleanup for bulky terminal-task data.

The task row, final report and trace events stay in place for audit.  Only the
large ephemeral artefacts (raw payload, checkpoints and collaboration messages)
are removed after the configured retention window.
"""
from datetime import datetime, timedelta, timezone
import logging
import threading
from typing import Any, Dict, Optional


logger = logging.getLogger("evoagent")


class DataRetention:
    def __init__(
        self, store, days: int, interval_seconds: int = 3600, limit: int = 1000,
        failure_case_days: int = 0, audit_days: int = 0, alert_days: int = 0,
    ):
        self.store = store
        self.days = max(0, int(days))
        self.failure_case_days = max(0, int(failure_case_days))
        self.audit_days = max(0, int(audit_days))
        self.alert_days = max(0, int(alert_days))
        self.interval_seconds = max(60, int(interval_seconds))
        self.limit = max(1, min(int(limit), 5000))
        self._stop = threading.Event()
        self._thread = None

    @property
    def enabled(self) -> bool:
        return any((
            self.days, self.failure_case_days,
            self.audit_days, self.alert_days,
        ))

    def _cutoff(self, days: int, now: Optional[datetime]) -> str:
        current = now or datetime.now(timezone.utc)
        return (current - timedelta(days=days)).isoformat()

    def cutoff(self, now: Optional[datetime] = None) -> str:
        return self._cutoff(self.days, now)

    def run_once(self, now: Optional[datetime] = None) -> Dict[str, int]:
        result = {
            "tasks": 0, "task_payloads": 0,
            "checkpoints": 0, "agent_messages": 0,
            "failure_cases": 0, "audit": 0, "alerts": 0,
        }
        if self.days:
            task_result = self.store.purge_expired_task_data(
                self.cutoff(now), self.limit,
            )
            result.update(task_result)
            if task_result.get("tasks"):
                logger.info(
                    "retention removed task data for %d task(s): %s",
                    task_result["tasks"], task_result,
                )
        for field, days, selector in (
            ("failure_cases", self.failure_case_days, {"failure_cases": True}),
            ("audit", self.audit_days, {"audit": True}),
            ("alerts", self.alert_days, {"alerts": True}),
        ):
            if not days:
                continue
            metadata = self.store.purge_expired_metadata(
                self._cutoff(days, now),
                failure_cases=selector.get("failure_cases", False),
                audit=selector.get("audit", False),
                alerts=selector.get("alerts", False),
            )
            result[field] = metadata.get(field, 0)
            if metadata.get(field):
                logger.info(
                    "retention removed %d %s row(s)", metadata[field], field,
                )
        return result

    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, name="evoagent-retention", daemon=True,
        )
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                logger.exception("retention cleanup failed")
            if self._stop.wait(self.interval_seconds):
                return

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout)
