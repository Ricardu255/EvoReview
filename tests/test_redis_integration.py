"""Real Redis lease renewal and duplicate delivery, enabled in CI."""
import json
import os
import threading
import time
import unittest
import uuid

from evoagent.adapters.task_queue import TaskQueue
from evoagent.errors import PermanentTaskError


def redis_url():
    return os.environ["EVOAGENT_TEST_REDIS_URL"]


def wait_until(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(.05)
    return predicate()


def isolated_queue(test, handler, workers=1, **options):
    suffix = uuid.uuid4().hex

    class IsolatedQueue(TaskQueue):
        STREAM = "evoagent:test:stream:" + suffix
        DLQ = "evoagent:test:dlq:" + suffix
        GROUP = "test-workers:" + suffix

    queue = IsolatedQueue(
        handler, workers=workers, redis_url=redis_url(), **options,
    )

    def cleanup():
        queue.close(timeout=5)
        client = queue._redis
        client.delete(queue.STREAM, queue.DLQ)

    test.addCleanup(cleanup)
    return queue, suffix


def envelope(message_id, tenant="integration", attempt=0):
    return {
        "message_id": message_id, "attempt": attempt,
        "payload": {"task_id": message_id, "tenant_id": tenant},
        "submitted_at": time.time(),
    }



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


@unittest.skipUnless(os.getenv("EVOAGENT_TEST_REDIS_URL"), "set EVOAGENT_TEST_REDIS_URL to run Redis integration")
class RedisDeliveryIntegrationTests(unittest.TestCase):
    def test_permanent_failure_is_dead_lettered_and_replayable(self):
        calls, state = [], {"fail": True}

        def handler(payload):
            calls.append(payload["task_id"])
            if state["fail"]:
                state["fail"] = False
                raise PermanentTaskError("permanent repository error")

        queue, suffix = isolated_queue(self, handler, max_attempts=3)
        message_id = "permanent:" + suffix
        queue.submit(
            {"task_id": message_id, "tenant_id": "integration"}, message_id,
        )

        self.assertTrue(wait_until(
            lambda: queue.dead_letters(10, "integration"), timeout=8,
        ))
        item = queue.dead_letters(10, "integration")[0]
        self.assertEqual(message_id, item["message_id"])
        self.assertIn("permanent repository error", item["error"])
        self.assertEqual(1, len(calls))

        self.assertTrue(queue.replay_dead_letter(message_id, "integration"))
        self.assertTrue(wait_until(lambda: len(calls) >= 2, timeout=8))
        self.assertTrue(wait_until(
            lambda: queue._redis.xpending(queue.STREAM, queue.GROUP)["pending"] == 0,
            timeout=8,
        ))
        self.assertEqual("redis-streams", queue.backend)

    def test_transient_retries_are_redelivered_then_dead_lettered(self):
        calls = []

        def handler(payload):
            calls.append(payload["task_id"])
            raise RuntimeError("transient outage")

        queue, suffix = isolated_queue(
            self, handler, max_attempts=2, lease_seconds=.2,
        )
        message_id = "retry:" + suffix
        queue.submit(
            {"task_id": message_id, "tenant_id": "integration"}, message_id,
        )

        self.assertTrue(wait_until(
            lambda: queue.dead_letters(10, "integration"), timeout=15,
        ))
        item = queue.dead_letters(10, "integration")[0]
        self.assertEqual(2, item["attempt"])
        self.assertIn("transient outage", item["error"])
        self.assertEqual(2, len(calls))
        self.assertTrue(wait_until(
            lambda: queue._redis.xpending(queue.STREAM, queue.GROUP)["pending"] == 0,
            timeout=8,
        ))

    def test_invalid_stream_entry_is_dead_lettered_and_acknowledged(self):
        queue, suffix = isolated_queue(self, lambda payload: None)
        queue._redis.xadd(queue.STREAM, {"envelope": "not-json"})

        self.assertTrue(wait_until(
            lambda: queue.dead_letters(10), timeout=8,
        ))
        item = queue.dead_letters(10)[0]
        self.assertIn("invalid queue envelope", item["error"])
        self.assertTrue(wait_until(
            lambda: queue._redis.xpending(queue.STREAM, queue.GROUP)["pending"] == 0,
            timeout=8,
        ))

    def test_orphaned_pending_entry_is_reclaimed_and_delivered(self):
        import redis as redis_module

        client = redis_module.Redis.from_url(redis_url(), decode_responses=True)
        suffix = uuid.uuid4().hex
        stream = "evoagent:test:stream:" + suffix
        dlq = "evoagent:test:dlq:" + suffix
        group = "test-workers:" + suffix
        message_id = "reclaim:" + suffix
        try:
            client.delete(stream, dlq)
            client.xgroup_create(stream, group, id="0", mkstream=True)
            client.xadd(stream, {
                "envelope": json.dumps(envelope(message_id)),
            })
            # Make the entry pending under a consumer that will never return.
            client.xreadgroup(group, "ghost-consumer", {stream: ">"}, count=1)

            delivered = threading.Event()

            class ReclaimQueue(TaskQueue):
                STREAM = stream
                DLQ = dlq
                GROUP = group

            queue = ReclaimQueue(
                lambda payload: delivered.set(), workers=1,
                redis_url=redis_url(), lease_seconds=.1,
            )
            try:
                self.assertTrue(delivered.wait(10))
                self.assertTrue(wait_until(
                    lambda: client.xpending(stream, group)["pending"] == 0,
                    timeout=8,
                ))
            finally:
                queue.close(timeout=5)
        finally:
            client.delete(stream, dlq)
            client.close()

    def test_dead_letters_are_tenant_filtered_and_limited(self):
        queue, suffix = isolated_queue(self, lambda payload: None)
        queue._dead_letter(envelope("tenant-a:" + suffix, tenant="a"), "a-error")
        queue._dead_letter(envelope("tenant-b:" + suffix, tenant="b"), "b-error")

        self.assertEqual(
            ["a"], [item["payload"]["tenant_id"]
                    for item in queue.dead_letters(10, "a")],
        )
        self.assertEqual(
            ["b"], [item["payload"]["tenant_id"]
                    for item in queue.dead_letters(10, "b")],
        )
        self.assertEqual(1, len(queue.dead_letters(1, "a")))
        self.assertEqual(2, len(queue.dead_letters(10)))
