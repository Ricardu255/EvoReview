"""Real Redis lease renewal and duplicate delivery, enabled in CI."""
import os
import threading
import time
import unittest
import uuid

from evoagent.adapters.task_queue import TaskQueue


@unittest.skipUnless(os.getenv("EVOAGENT_TEST_REDIS_URL"), "set EVOAGENT_TEST_REDIS_URL to run Redis integration")
class RedisIntegrationTests(unittest.TestCase):
    def test_long_running_task_is_renewed_and_logical_duplicates_execute_once(self):
        suffix = uuid.uuid4().hex

        class IsolatedQueue(TaskQueue):
            STREAM = "evoagent:test:stream:" + suffix
            DLQ = "evoagent:test:dlq:" + suffix
            GROUP = "test-workers:" + suffix

        calls, lock = [], threading.Lock()
        started, finished = threading.Event(), threading.Event()

        def handler(_payload):
            with lock:
                calls.append(1)
            started.set()
            # Longer than three leases: a second worker must not reclaim this.
            time.sleep(.8)
            finished.set()

        first = IsolatedQueue(handler, workers=1, redis_url=os.environ["EVOAGENT_TEST_REDIS_URL"], lease_seconds=.2)
        second = IsolatedQueue(handler, workers=1, redis_url=os.environ["EVOAGENT_TEST_REDIS_URL"], lease_seconds=.2)
        message_id = "integration:" + suffix
        payload = {"task_id": message_id, "tenant_id": "integration"}
        try:
            first.submit(payload, message_id)
            self.assertTrue(started.wait(5))
            second.submit(payload, message_id)
            self.assertTrue(finished.wait(5))
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                pending = first._redis.xpending(first.STREAM, first.GROUP)
                if pending["pending"] == 0:
                    break
                time.sleep(.05)
            self.assertEqual(0, first._redis.xpending(first.STREAM, first.GROUP)["pending"])
            self.assertEqual(1, len(calls))
            self.assertEqual([], first.dead_letters())
        finally:
            first.close()
            second.close()
            key = first._delivery_key({"message_id": message_id, "payload": payload})
            first._redis.delete(first.STREAM, first.DLQ, "evoagent:delivery:lock:" + key, "evoagent:delivery:done:" + key)
