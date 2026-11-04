"""Tests for the periodic retention cleaner."""
from datetime import datetime, timezone
import threading
import unittest

from evoagent.app.maintenance import DataRetention


class _Store:
    def __init__(self):
        self.calls = []
        self.called = threading.Event()
        self.result = {
            "tasks": 1, "task_payloads": 1,
            "checkpoints": 1, "agent_messages": 1,
        }

    def purge_expired_task_data(self, before, limit):
        self.calls.append((before, limit))
        self.called.set()
        return dict(self.result)


class DataRetentionTests(unittest.TestCase):
    def test_disabled_retention_never_touches_the_store(self):
        store = _Store()
        retention = DataRetention(store, days=0)
        self.assertFalse(retention.enabled)
        self.assertEqual(
            {"tasks": 0, "task_payloads": 0, "checkpoints": 0, "agent_messages": 0},
            retention.run_once(),
        )
        self.assertEqual([], store.calls)

    def test_run_once_uses_the_configured_window_and_limit(self):
        store = _Store()
        retention = DataRetention(store, days=30, limit=500)
        now = datetime(2026, 5, 1, tzinfo=timezone.utc)
        result = retention.run_once(now)
        self.assertEqual(1, result["tasks"])
        self.assertEqual(1, len(store.calls))
        self.assertTrue(store.calls[0][0].startswith("2026-04-01"))
        self.assertEqual(500, store.calls[0][1])

    def test_start_runs_cleanup_once_and_stop_joins(self):
        store = _Store()
        retention = DataRetention(store, days=1, interval_seconds=60)
        retention.start()
        self.assertTrue(store.called.wait(2))
        retention.stop()
        self.assertGreaterEqual(len(store.calls), 1)


if __name__ == "__main__":
    unittest.main()
