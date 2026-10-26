"""Lease, duplicate-delivery and recovery behavior without external services."""
import copy
import json
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from evoagent.task_queue import DeliveryBusy, TaskQueue


class SimulatedRedis:
    def __init__(self):
        self.values = {}
        self.lock = threading.RLock()
        self.entries = []
        self.acks = []
        self.claims = []
        self.renewed = threading.Event()
        self.fail_renewal = False
        self.envelopes = []

    def get(self, key):
        with self.lock:
            value = self.values.get(key)
            if not value or value[1] <= time.monotonic():
                self.values.pop(key, None)
                return None
            return value[0]

    def set(self, key, value, nx=False, px=None, ex=None):
        with self.lock:
            if nx and self.get(key) is not None:
                return False
            self.values[key] = (value, time.monotonic() + (px / 1000 if px else ex or 86400))
            return True

    def eval(self, script, count, *args):
        with self.lock:
            keys, values = args[:count], args[count:]
            if self.get(keys[0]) != values[0]:
                return 0
            if script == TaskQueue.RENEW_LOCK:
                self.renewed.set()
                if self.fail_renewal:
                    return 0
                self.set(keys[0], values[0], px=int(values[1]))
            elif script == TaskQueue.RELEASE_LOCK:
                self.values.pop(keys[0], None)
            elif script == TaskQueue.COMPLETE:
                self.set(keys[1], "1", ex=int(values[1]))
            else:
                raise AssertionError("unexpected script")
            return 1

    def xclaim(self, *args, **kwargs):
        self.claims.append(args[4])
        return args[4]

    def xautoclaim(self, *args, **kwargs):
        with self.lock:
            entries, self.entries = self.entries[:1], self.entries[1:]
            return "0-0", entries, []

    def xack(self, *args):
        self.acks.append(args[-1])

    def xadd(self, stream, fields):
        self.envelopes.append(json.loads(fields["envelope"]))
        return "2-0"


def envelope(tenant="a"):
    return {"message_id": "same-task", "attempt": 0, "payload": {"task_id": "same-task", "tenant_id": tenant}}


class QueueDeliveryTests(unittest.TestCase):
    def queue(self, handler, redis=None, **options):
        queue = TaskQueue(handler, workers=1, lease_seconds=.12, **options)
        self.addCleanup(queue.close)
        queue._redis = redis
        return queue

    def test_active_delivery_is_renewed_and_reclaimer_cannot_duplicate_it(self):
        redis = SimulatedRedis()
        started, finish = threading.Event(), threading.Event()
        calls = []

        def handler(payload):
            calls.append(payload)
            started.set()
            finish.wait(3)

        first, second = self.queue(handler, redis), self.queue(handler, redis)
        with ThreadPoolExecutor(max_workers=1) as pool:
            running = pool.submit(first._deliver, envelope(), "1-0")
            try:
                self.assertTrue(started.wait(1))
                self.assertTrue(redis.renewed.wait(1))
                redis.entries = [("1-0", {"envelope": json.dumps(envelope())})]
                second._reclaim_stale()
                self.assertEqual(1, len(calls))
                self.assertEqual([], redis.acks)
                # Cross several lease periods; healthy renewal keeps ownership.
                time.sleep(.28)
                with self.assertRaises(DeliveryBusy):
                    second._deliver(envelope(), "2-0")
            finally:
                finish.set()
            self.assertTrue(running.result(timeout=2))
        self.assertTrue(redis.claims)
        self.assertTrue(second._deliver(envelope(), "2-0"))
        self.assertEqual(1, len(calls))

    def test_lost_lease_is_neither_completed_nor_acknowledged(self):
        redis = SimulatedRedis()
        redis.fail_renewal = True

        def handler(_payload):
            self.assertTrue(redis.renewed.wait(1))

        queue = self.queue(handler, redis)
        redis.entries = [("1-0", {"envelope": json.dumps(envelope())})]
        queue._reclaim_stale()
        self.assertEqual([], redis.acks)
        key = queue._delivery_key(envelope())
        self.assertIsNone(redis.get("evoagent:delivery:done:" + key))

    def test_completed_delivery_is_deduplicated_but_another_tenant_is_independent(self):
        calls = []
        for redis in (None, SimulatedRedis()):
            queue = self.queue(lambda payload: calls.append(payload["tenant_id"]), redis)
            self.assertTrue(queue._deliver(envelope("a")))
            self.assertTrue(queue._deliver(envelope("a")))
            self.assertTrue(queue._deliver(envelope("b")))
        self.assertEqual(["a", "b", "a", "b"], calls)

    def test_failed_attempt_requeues_and_does_not_mark_completion(self):
        redis = SimulatedRedis()
        attempts = []

        def handler(_payload):
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("transient")

        queue = self.queue(handler, redis)
        self.assertFalse(queue._deliver(envelope()))
        self.assertEqual(1, redis.envelopes[0]["attempt"])
        self.assertTrue(queue._deliver(redis.envelopes[0]))
        self.assertTrue(queue._deliver(envelope()))
        self.assertEqual(2, len(attempts))

    def test_redis_worker_recovers_after_temporary_connection_failure(self):
        redis = SimulatedRedis()
        queue = self.queue(lambda _payload: None, redis)
        reads = []

        def read(*args, **kwargs):
            reads.append(1)
            if len(reads) == 1:
                raise ConnectionError("temporary disconnect")
            queue._stop.set()
            return []

        redis.xreadgroup = read
        queue._redis_worker()
        self.assertEqual(2, len(reads))

    def test_redis_dead_letter_filter_paginates_past_other_tenants(self):
        redis = SimulatedRedis()
        queue = self.queue(lambda _payload: None, redis)
        pages = [
            [("2-0", {"envelope": json.dumps(envelope("b"))})],
            [("1-0", {"envelope": json.dumps(envelope("a"))})],
        ]
        redis.xrevrange = lambda *args, **kwargs: pages.pop(0) if pages else []
        self.assertEqual("a", queue.dead_letters(1, "a")[0]["payload"]["tenant_id"])
