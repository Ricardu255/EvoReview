"""Branch-level tests for TaskQueue that do not require a real Redis server."""
import json
import sys
import threading
import time
import types
import unittest
from unittest.mock import MagicMock, patch

from evoagent.adapters.task_queue import DeliveryBusy, TaskQueue
from evoagent.errors import PermanentTaskError, TaskCancelled


def envelope(tenant="a", message_id="m1", attempt=0):
    return {
        "message_id": message_id, "attempt": attempt,
        "payload": {"task_id": message_id, "tenant_id": tenant},
    }


class QueueTestCase(unittest.TestCase):
    def memory_queue(self, handler=None, **options):
        queue = TaskQueue(handler or (lambda payload: None), workers=1, **options)
        self.addCleanup(queue.close, False)
        return queue


class RedisConstructionTests(QueueTestCase):
    def test_missing_redis_package_is_reported(self):
        with patch.dict(sys.modules, {"redis": None}):
            with self.assertRaisesRegex(RuntimeError, "pip install redis"):
                TaskQueue(lambda payload: None, redis_url="redis://127.0.0.1:1/0")

    def fake_module(self, error):
        class ResponseError(Exception):
            pass

        class FakeRedis:
            def __init__(self):
                self.acks = []

            def ping(self):
                return True

            def xgroup_create(self, *args, **kwargs):
                if error:
                    raise ResponseError(error)

            def xautoclaim(self, *args, **kwargs):
                return ("0-0", [], [])

            def xreadgroup(self, *args, **kwargs):
                return []

            def xadd(self, *args, **kwargs):
                return "1-0"

            def xack(self, *args):
                self.acks.append(args[-1])

        return types.SimpleNamespace(
            Redis=types.SimpleNamespace(
                from_url=lambda url, decode_responses=True: FakeRedis(),
            ),
            ResponseError=ResponseError,
        )

    def test_busygroup_is_tolerated_and_backend_is_reported(self):
        with patch.dict(sys.modules, {"redis": self.fake_module("BUSYGROUP exists")}):
            queue = TaskQueue(lambda payload: None, workers=1,
                              redis_url="redis://fake/0")
        self.assertEqual("redis-streams", queue.backend)
        queue.close(wait=False)

    def test_other_group_creation_errors_propagate(self):
        with patch.dict(sys.modules, {"redis": self.fake_module("ERR denied")}):
            with self.assertRaises(Exception) as caught:
                TaskQueue(lambda payload: None, workers=1,
                          redis_url="redis://fake/0")
        self.assertIn("denied", str(caught.exception))


class LifecycleTests(QueueTestCase):
    def test_memory_backend_and_submit_after_close(self):
        queue = TaskQueue(lambda payload: None, workers=1)
        self.assertEqual("memory-acked", queue.backend)
        queue.close(wait=False)
        self.assertTrue(queue._stop.is_set())
        self.assertEqual("after-close", queue.submit({"task_id": "after-close"}))
        self.assertEqual(0, queue._memory.qsize())

    def test_memory_worker_exits_when_shutdown_during_idle_and_dequeue(self):
        idle = self.memory_queue()
        idle._stop.set()
        idle._memory_worker()  # empty queue + stop -> returns

        looping = TaskQueue(lambda payload: None, workers=1)
        self.addCleanup(looping.close, False)
        worker = threading.Thread(target=looping._memory_worker, daemon=True)
        worker.start()
        time.sleep(.6)  # one idle timeout -> continue, then block again
        looping._stop.set()
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())

        dequeued = TaskQueue(lambda payload: None, workers=1)
        self.addCleanup(dequeued.close, False)
        dequeued._memory.put(envelope())
        dequeued._stop.set()
        dequeued._memory_worker()  # stop set after dequeue -> returns without delivery
        self.assertEqual(0, dequeued._memory.qsize())

    def test_memory_delivery_failure_is_logged_and_worker_survives(self):
        queue = self.memory_queue()
        failures = []

        def broken(_envelope):
            failures.append(1)
            raise RuntimeError("unexpected")

        queue._deliver = broken
        queue._ensure_memory_workers()
        queue._memory.put(envelope())
        deadline = time.monotonic() + 2
        while not failures and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual([1], failures)
        self.assertTrue(queue._memory_workers[0].is_alive())


class DeliveryOwnershipTests(QueueTestCase):
    def test_memory_claim_deduplicates_and_rejects_concurrent_owner(self):
        queue = self.memory_queue()
        item = envelope()
        with queue._delivery_claim(item) as claim:
            self.assertEqual(queue._delivery_key(item), claim["key"])
            with self.assertRaises(DeliveryBusy):
                with queue._delivery_claim(item):
                    pass
            queue._mark_completed(claim)
        with queue._delivery_claim(item) as cached:
            self.assertIsNone(cached)

    def test_owned_delivery_handles_each_outcome(self):
        queue = self.memory_queue()
        claim = {"key": "k"}

        def deliver(handler):
            queue.handler = handler
            return queue._deliver_owned(envelope(), claim)

        self.assertTrue(deliver(lambda payload: None))

        with self.assertRaises(DeliveryBusy):
            deliver(lambda payload: (_ for _ in ()).throw(DeliveryBusy("busy")))

        self.assertTrue(deliver(
            lambda payload: (_ for _ in ()).throw(TaskCancelled("cancelled"))
        ))
        self.assertEqual([], queue.dead_letters())

        dead = []
        queue.on_dead_letter = lambda payload, error: dead.append(error)
        self.assertFalse(deliver(
            lambda payload: (_ for _ in ()).throw(PermanentTaskError("permanent"))
        ))
        self.assertEqual(["permanent"], dead)
        self.assertEqual(1, len(queue.dead_letters()))

    def test_transient_failure_retries_and_exhaustion_dead_letters(self):
        queue = self.memory_queue(max_attempts=2)
        timers = []

        class Timer:
            def __init__(self, delay, function, args=()):
                self.delay, self.function, self.args = delay, function, args
                self.daemon = False
                timers.append(self)

            def start(self):
                pass

        def handler(payload):
            raise RuntimeError("transient")

        queue.handler = handler
        with patch("evoagent.adapters.task_queue.threading.Timer", Timer):
            self.assertFalse(queue._deliver_owned(envelope(attempt=0), {"key": "k1"}))
        self.assertEqual(1, len(timers))
        self.assertGreater(timers[0].delay, 0)
        self.assertEqual(1, timers[0].args[0]["attempt"])

        self.assertFalse(queue._deliver_owned(envelope(attempt=1), {"key": "k2"}))
        self.assertEqual(1, len(queue.dead_letters()))
        self.assertIn("transient", queue.dead_letters()[0]["error"])

    def test_memory_dead_letter_queue_honours_its_limit(self):
        queue = self.memory_queue(dlq_maxlen=2)
        for index in range(3):
            queue._dead_letter(
                envelope(message_id="m%d" % index), "error-%d" % index,
            )
        items = queue.dead_letters()
        self.assertEqual(["m2", "m1"], [item["message_id"] for item in items])

    def test_memory_replay_removes_the_dead_letter(self):
        queue = self.memory_queue()
        queue._dead_letter(envelope(message_id="replay-memory"), "error")
        self.assertEqual(1, len(queue.dead_letters()))
        self.assertTrue(queue.replay_dead_letter("replay-memory", "a"))
        self.assertEqual([], queue.dead_letters())

    def test_completed_history_is_bounded(self):
        queue = self.memory_queue()
        queue._completed.update({str(index): time.time() + 100 for index in range(10001)})
        queue._mark_completed({"key": "new"})
        self.assertEqual(10000, len(queue._completed))
        self.assertIn("new", queue._completed)

    def test_retry_wait_and_memory_resubmission(self):
        queue = self.memory_queue()
        self.assertTrue(queue._wait_for_retry({"retry_after": 0}))
        queue._stop.set()
        self.assertFalse(queue._wait_for_retry({"retry_after": 0}))
        self.assertFalse(queue._wait_for_retry({"retry_after": time.time() + 5}))

        queue = self.memory_queue()
        submitted = []
        queue._submit_delivery = submitted.append
        queue._submit_memory(envelope())
        self.assertEqual(1, len(submitted))
        queue._stop.set()
        queue._submit_memory(envelope())
        self.assertEqual(1, len(submitted))


class _RedisDouble:
    def __init__(self, renew_result=1, eval_raises=False, release_raises=False):
        self.values = {}
        self.renew_result = renew_result
        self.eval_raises = eval_raises
        self.release_raises = release_raises
        self.claims = []
        self.dlq = []
        self.stream_entries = []
        self.acks = []
        self.deleted = []
        self.trimmed = []
        self.length = 0
        self.pending = {"pending": 0, "min": None}
        self.last_delivered = "0-0"
        self.complete_result = 1
        self.renewed = threading.Event()

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, nx=False, px=None):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    def eval(self, script, count, *args):
        if self.eval_raises:
            raise RuntimeError("redis down")
        if script == TaskQueue.RENEW_LOCK:
            self.renewed.set()
            return self.renew_result
        if script == TaskQueue.RELEASE_LOCK:
            if self.release_raises:
                raise RuntimeError("release failed")
            return 1
        if script == TaskQueue.COMPLETE:
            return self.complete_result
        raise AssertionError("unexpected script")

    def xclaim(self, *args, **kwargs):
        self.claims.append(args[4])
        return args[4]

    def xautoclaim(self, *args, **kwargs):
        return ("0-0", [], [])

    def xreadgroup(self, *args, **kwargs):
        entries, self.stream_entries = self.stream_entries, []
        return [("stream", entries)] if entries else []

    def xadd(self, stream, fields, **kwargs):
        self.dlq.append(json.loads(fields["envelope"]))
        return "%d-0" % len(self.dlq)

    def xack(self, *args):
        self.acks.append(args[-1])

    def xdel(self, stream, *ids):
        self.deleted.extend(ids)

    def xlen(self, stream):
        return self.length

    def xpending(self, stream, group):
        return dict(self.pending)

    def xinfo_groups(self, stream):
        return [{
            "name": TaskQueue.GROUP, "last-delivered-id": self.last_delivered,
        }]

    def xtrim(self, stream, minid=None, approximate=False, **kwargs):
        self.trimmed.append((stream, minid))

    def xrevrange(self, stream, max="+", count=100, **kwargs):
        rows = [
            ("%d-0" % (index + 1), {"envelope": json.dumps(item)})
            for index, item in enumerate(self.dlq)
        ]
        rows.reverse()
        if str(max).startswith("("):
            cutoff = int(str(max)[1:].split("-")[0])
            rows = [row for row in rows if int(row[0].split("-")[0]) < cutoff]
        return rows[:count]


class RedisClaimTests(QueueTestCase):
    def redis_queue(self, redis):
        queue = self.memory_queue()
        queue._redis = redis
        queue.lease_seconds = 0.1
        return queue

    def test_completed_redis_delivery_is_skipped(self):
        redis = _RedisDouble()
        queue = self.redis_queue(redis)
        item = envelope()
        redis.values["evoagent:delivery:done:" + queue._delivery_key(item)] = "1"
        with queue._delivery_claim(item) as claim:
            self.assertIsNone(claim)

    def test_lock_contention_raises_delivery_busy(self):
        redis = _RedisDouble()
        queue = self.redis_queue(redis)
        item = envelope()
        redis.values["evoagent:delivery:lock:" + queue._delivery_key(item)] = "other"
        with self.assertRaises(DeliveryBusy):
            with queue._delivery_claim(item):
                pass

    def test_lost_lease_is_detected(self):
        redis = _RedisDouble(renew_result=0)
        queue = self.redis_queue(redis)
        with queue._delivery_claim(envelope(), "1-0") as claim:
            deadline = time.monotonic() + 2
            while not claim["lost"].is_set() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(claim["lost"].is_set())
        self.assertEqual([], redis.claims)
        with self.assertRaises(DeliveryBusy):
            queue._mark_completed(claim)

    def test_healthy_renewal_reclaims_the_stream_entry(self):
        redis = _RedisDouble()
        queue = self.redis_queue(redis)
        with queue._delivery_claim(envelope(), "5-0") as claim:
            self.assertTrue(redis.renewed.wait(2))
            self.assertFalse(claim["lost"].is_set())
        self.assertEqual([["5-0"]], redis.claims)

    def test_renewal_errors_are_treated_as_lost_leases(self):
        redis = _RedisDouble(eval_raises=True)
        queue = self.redis_queue(redis)
        with queue._delivery_claim(envelope()) as claim:
            deadline = time.monotonic() + 2
            while not claim["lost"].is_set() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(claim["lost"].is_set())

    def test_release_errors_do_not_escape(self):
        redis = _RedisDouble(release_raises=True)
        queue = self.redis_queue(redis)
        with queue._delivery_claim(envelope()) as claim:
            self.assertIsNotNone(claim)

    def test_completion_failure_raises(self):
        redis = _RedisDouble()
        redis.complete_result = 0
        queue = self.redis_queue(redis)
        with self.assertRaises(DeliveryBusy):
            queue._mark_completed({
                "lost": threading.Event(), "lock_key": "l", "done_key": "d",
                "token": "t",
            })


class RedisWorkerTests(QueueTestCase):
    def redis_queue(self, redis):
        queue = self.memory_queue()
        queue._redis = redis
        queue.lease_seconds = 0.1
        return queue

    def test_invalid_envelope_is_dead_lettered_and_acked(self):
        redis = _RedisDouble()
        queue = self.redis_queue(redis)
        redis.stream_entries = [("3-0", {"envelope": "not-json"})]
        original = redis.xreadgroup
        calls = []

        def read(*args, **kwargs):
            calls.append(1)
            if len(calls) > 1:
                queue._stop.set()
                return []
            return original(*args, **kwargs)

        redis.xreadgroup = read
        queue._redis_worker()
        self.assertEqual(["3-0"], redis.acks)
        self.assertIn("invalid queue envelope", redis.dlq[0]["error"])

    def test_future_retry_after_interrupts_the_worker(self):
        redis = _RedisDouble()
        queue = self.redis_queue(redis)
        redis.stream_entries = [("4-0", {
            "envelope": json.dumps({
                **envelope(), "retry_after": time.time() + 60,
            }),
        })]
        read = threading.Event()
        original = redis.xreadgroup

        def read_once(*args, **kwargs):
            result = original(*args, **kwargs)
            read.set()
            return result

        redis.xreadgroup = read_once
        worker = threading.Thread(target=queue._redis_worker, daemon=True)
        worker.start()
        self.assertTrue(read.wait(2))
        queue._stop.set()
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual([], redis.acks)

    def test_dead_letter_redis_branch_filters_and_replays(self):
        redis = _RedisDouble()
        queue = self.redis_queue(redis)
        queue._dead_letter(envelope("a", "ma"), "a-error")
        queue._dead_letter(envelope("b", "mb"), "b-error")
        self.assertEqual(["a"], [item["payload"]["tenant_id"]
                                 for item in queue.dead_letters(10, "a")])
        submitted = []
        queue.submit = lambda payload, message_id="": submitted.append(
            (payload, message_id)) or message_id
        self.assertTrue(queue.replay_dead_letter("ma", "a"))
        self.assertEqual([({"task_id": "ma", "tenant_id": "a"}, "ma")], submitted)
        self.assertEqual(1, len(redis.deleted))
        self.assertFalse(queue.replay_dead_letter("missing", "a"))

    def test_trim_uses_oldest_pending_id(self):
        redis = _RedisDouble()
        queue = self.redis_queue(redis)
        queue.stream_maxlen = 100
        redis.length = 101
        redis.pending = {"pending": 1, "min": "50-0"}
        queue._maybe_trim()
        self.assertEqual([(TaskQueue.STREAM, "50-0")], redis.trimmed)

    def test_trim_uses_last_delivered_id_when_no_pending(self):
        redis = _RedisDouble()
        queue = self.redis_queue(redis)
        queue.stream_maxlen = 100
        redis.length = 101
        redis.pending = {"pending": 0, "min": None}
        redis.last_delivered = "75-0"
        queue._maybe_trim()
        self.assertEqual([(TaskQueue.STREAM, "75-0")], redis.trimmed)

    def test_trim_is_skipped_below_the_limit(self):
        redis = _RedisDouble()
        queue = self.redis_queue(redis)
        queue.stream_maxlen = 100
        redis.length = 100
        queue._maybe_trim()
        self.assertEqual([], redis.trimmed)

    def test_reclaim_invalid_envelope_is_dead_lettered_and_acked(self):
        redis = _RedisDouble()
        redis.xautoclaim = lambda *args, **kwargs: (
            "0-0", [("9-0", {"envelope": "not-json"})], [],
        )
        queue = self.redis_queue(redis)
        queue._reclaim_stale()
        self.assertEqual(["9-0"], redis.acks)
        self.assertEqual(1, len(redis.dlq))
        self.assertIn("invalid replayed queue envelope", redis.dlq[0]["error"])

    def test_reclaim_failures_are_ignored(self):
        redis = _RedisDouble()

        def broken(*args, **kwargs):
            raise RuntimeError("no xautoclaim")

        redis.xautoclaim = broken
        queue = self.redis_queue(redis)
        queue._reclaim_stale()


if __name__ == "__main__":
    unittest.main()
