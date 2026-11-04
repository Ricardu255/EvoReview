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
    ):
        self.store = store
        self.days = max(0, int(days))
        self.interval_seconds = max(60, int(interval_seconds))
        self.limit = max(1, min(int(limit), 5000))
        self._stop = threading.Event()
        self._thread = None

    @property
    def enabled(self) -> bool:
        return self.days > 0

    def cutoff(self, now: Optional[datetime] = None) -> str:
        current = now or datetime.now(timezone.utc)
        return (current - timedelta(days=self.days)).isoformat()

    def run_once(self, now: Optional[datetime] = None) -> Dict[str, int]:
        if not self.enabled:
            return {
                "tasks": 0, "task_payloads": 0,
                "checkpoints": 0, "agent_messages": 0,
            }
        result = self.store.purge_expired_task_data(self.cutoff(now), self.limit)
        if result.get("tasks"):
            logger.info(
                "retention removed task data for %d task(s): %s",
                result["tasks"], result,
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
