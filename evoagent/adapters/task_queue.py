"""Durable task delivery with ACK, leases, retry backoff and a dead-letter queue."""
import json
import hashlib
from collections import OrderedDict
from contextlib import contextmanager
import queue
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, Optional

from ..errors import PermanentTaskError


class DeliveryBusy(RuntimeError):
    """Another worker owns this logical delivery; leave the stream entry pending."""


class TaskQueue:
    STREAM = "evoagent:review:stream"
    DLQ = "evoagent:review:dlq"
    GROUP = "evoagent-workers"
    RENEW_LOCK = "if redis.call('get',KEYS[1]) == ARGV[1] then return redis.call('pexpire',KEYS[1],ARGV[2]) else return 0 end"
    RELEASE_LOCK = "if redis.call('get',KEYS[1]) == ARGV[1] then return redis.call('del',KEYS[1]) else return 0 end"
    COMPLETE = "if redis.call('get',KEYS[1]) == ARGV[1] then redis.call('set',KEYS[2],'1','EX',ARGV[2]); return 1 else return 0 end"

    def __init__(
        self, handler: Callable[[Dict[str, Any]], None], workers: int = 2,
        redis_url: str = "", max_attempts: int = 3, lease_seconds: int = 60,
        on_dead_letter: Optional[Callable[[Dict[str, Any], str], None]] = None,
    ):
        self.handler = handler
        self.redis_url = redis_url
        self.max_attempts = max_attempts
        self.lease_seconds = lease_seconds
        self.on_dead_letter = on_dead_letter
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="evoagent-worker")
        self._redis = None
        self._memory: queue.Queue = queue.Queue()
        self._memory_dlq = []
        self._active = set()
        self._completed = OrderedDict()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.consumer = "%s-%s" % (socket.gethostname(), uuid.uuid4().hex[:8])
        if redis_url:
            try:
                import redis
            except ImportError as exc:
                raise RuntimeError("Redis mode requires: pip install redis") from exc
            self._redis = redis.Redis.from_url(redis_url, decode_responses=True)
            self._redis.ping()
            try:
                self._redis.xgroup_create(self.STREAM, self.GROUP, id="0", mkstream=True)
            except redis.ResponseError as exc:
                if "BUSYGROUP" not in str(exc):
                    raise
            for _ in range(workers):
                self._executor.submit(self._redis_worker)

    @property
    def backend(self) -> str:
        return "redis-streams" if self._redis else "memory-acked"

    def submit(self, payload: Dict[str, Any], message_id: str = "") -> str:
        envelope = {
            "message_id": message_id or str(payload.get("task_id") or uuid.uuid4()),
            "attempt": 0,
            "payload": payload,
            "submitted_at": time.time(),
        }
        if self._redis:
            self._redis.xadd(self.STREAM, {"envelope": json.dumps(envelope, ensure_ascii=False)})
        else:
            self._executor.submit(self._deliver, envelope)
        return envelope["message_id"]

    @staticmethod
    def _delivery_key(envelope):
        payload = envelope.get("payload") or {}
        return hashlib.sha256(json.dumps([
            payload.get("tenant_id", "default"), envelope["message_id"],
        ], separators=(",", ":")).encode("utf-8")).hexdigest()

    @contextmanager
    def _delivery_claim(self, envelope, redis_id=""):
        key = self._delivery_key(envelope)
        if not self._redis:
            with self._lock:
                if self._completed.get(key, 0) > time.time():
                    cached = True
                else:
                    cached = False
                    if key in self._active:
                        raise DeliveryBusy("delivery is already running")
                    self._active.add(key)
            try:
                yield None if cached else {"key": key}
            finally:
                if not cached:
                    with self._lock:
                        self._active.discard(key)
            return
        lock_key, done_key = "evoagent:delivery:lock:" + key, "evoagent:delivery:done:" + key
        if self._redis.get(done_key):
            yield None
            return
        token = uuid.uuid4().hex
        lease_ms = max(100, int(self.lease_seconds * 1000))
        if not self._redis.set(lock_key, token, nx=True, px=lease_ms):
            raise DeliveryBusy("delivery is owned by another worker")
        stopped, lost = threading.Event(), threading.Event()

        def renew():
            while not stopped.wait(lease_ms / 3000):
                try:
                    if not self._redis.eval(self.RENEW_LOCK, 1, lock_key, token, lease_ms):
                        lost.set()
                        return
                    if redis_id:
                        self._redis.xclaim(self.STREAM, self.GROUP, self.consumer, 0, [redis_id], justid=True)
                except Exception:
                    lost.set()
                    return

        heartbeat = threading.Thread(target=renew, name="evoagent-lease", daemon=True)
        heartbeat.start()
        try:
            yield None if self._redis.get(done_key) else {
                "key": key, "lock_key": lock_key, "done_key": done_key,
                "token": token, "lost": lost,
            }
        finally:
            stopped.set()
            heartbeat.join(timeout=1)
            try:
                self._redis.eval(self.RELEASE_LOCK, 1, lock_key, token)
            except Exception:
                pass  # An abandoned lock expires; never delete a new owner's lock.

    def _mark_completed(self, claim):
        if self._redis:
            if claim["lost"].is_set() or not self._redis.eval(
                self.COMPLETE, 2, claim["lock_key"], claim["done_key"], claim["token"], 86400,
            ):
                raise DeliveryBusy("delivery lease was lost before completion")
        else:
            with self._lock:
                self._completed[claim["key"]] = time.time() + 86400
                while len(self._completed) > 10000:
                    self._completed.popitem(last=False)

    def _deliver(self, envelope: Dict[str, Any], redis_id: str = "") -> bool:
        with self._delivery_claim(envelope, redis_id) as claim:
            if claim is None:
                return True
            return self._deliver_owned(envelope, claim)

    def _deliver_owned(self, envelope, claim) -> bool:
        envelope["attempt"] = int(envelope.get("attempt", 0)) + 1
        try:
            self.handler(envelope["payload"])
        except DeliveryBusy:
            raise
        except PermanentTaskError as exc:
            self._dead_letter(envelope, str(exc))
            return False
        except Exception as exc:
            if envelope["attempt"] >= self.max_attempts:
                self._dead_letter(envelope, str(exc))
            elif self._redis:
                self._redis.xadd(self.STREAM, {
                    "envelope": json.dumps(envelope, ensure_ascii=False)
                })
            else:
                delay = min(2 ** (envelope["attempt"] - 1), 10)
                timer = threading.Timer(delay, self._submit_memory, args=(envelope,))
                timer.daemon = True
                timer.start()
            return False
        self._mark_completed(claim)
        return True

    def _submit_memory(self, envelope: Dict[str, Any]) -> None:
        if not self._stop.is_set():
            self._executor.submit(self._deliver, envelope)

    def _redis_worker(self) -> None:
        while not self._stop.is_set():
            try:
                self._reclaim_stale()
                messages = self._redis.xreadgroup(
                    self.GROUP, self.consumer, {self.STREAM: ">"}, count=1, block=1000
                )
            except Exception:
                self._stop.wait(1)
                continue
            for _stream, entries in messages:
                for redis_id, fields in entries:
                    try:
                        envelope = json.loads(fields["envelope"])
                    except Exception as exc:
                        envelope = {
                            "message_id": redis_id, "attempt": self.max_attempts,
                            "payload": {}, "submitted_at": time.time(),
                        }
                        self._dead_letter(envelope, "invalid queue envelope: %s" % exc)
                        self._redis.xack(self.STREAM, self.GROUP, redis_id)
                        continue
                    try:
                        self._deliver(envelope, redis_id)
                        # ACK only after work completed or was safely requeued/DLQed.
                        self._redis.xack(self.STREAM, self.GROUP, redis_id)
                    except Exception:
                        # Infrastructure failure: leave pending for lease recovery.
                        continue

    def _reclaim_stale(self) -> None:
        try:
            result = self._redis.xautoclaim(
                self.STREAM, self.GROUP, self.consumer,
                min_idle_time=int(self.lease_seconds * 1000), start_id="0-0", count=1,
            )
            entries = result[1] if len(result) > 1 else []
            for redis_id, fields in entries:
                envelope = json.loads(fields["envelope"])
                self._deliver(envelope, redis_id)
                self._redis.xack(self.STREAM, self.GROUP, redis_id)
        except Exception:
            # Redis versions without XAUTOCLAIM still process new entries.
            return

    def _dead_letter(self, envelope: Dict[str, Any], error: str) -> None:
        item = {**envelope, "error": error[:2000], "failed_at": time.time()}
        if self._redis:
            self._redis.xadd(self.DLQ, {"envelope": json.dumps(item, ensure_ascii=False)})
        else:
            with self._lock:
                self._memory_dlq.append(item)
        if self.on_dead_letter:
            self.on_dead_letter(envelope.get("payload") or {}, item["error"])

    def dead_letters(self, limit: int = 100, tenant_id: Optional[str] = None) -> list:
        limit = max(1, min(int(limit), 500))
        if self._redis:
            values = []
            cursor = "+"
            while len(values) < limit:
                rows = self._redis.xrevrange(self.DLQ, max=cursor, count=100)
                if not rows:
                    break
                for _id, fields in rows:
                    item = json.loads(fields["envelope"])
                    if tenant_id is None or (item.get("payload") or {}).get("tenant_id", "default") == tenant_id:
                        values.append(item)
                cursor = "(" + rows[-1][0]
            return values[:limit]
        with self._lock:
            return [item for item in reversed(self._memory_dlq)
                    if tenant_id is None or (item.get("payload") or {}).get("tenant_id", "default") == tenant_id][:limit]

    def replay_dead_letter(self, message_id: str, tenant_id: Optional[str] = None) -> bool:
        for item in self.dead_letters(500, tenant_id):
            if item.get("message_id") == message_id:
                payload = item.get("payload") or {}
                self.submit(payload, message_id=message_id)
                return True
        return False

    def close(self, wait: bool = True) -> None:
        """Stop delivery.  By default, drain in-flight work and cancel queued items.

        Callers that shut the process down — or delete the store, like tests —
        must not race a worker thread that still holds an open connection;
        pass ``wait=False`` only when returning immediately matters more than
        a clean teardown.
        """
        self._stop.set()
        self._executor.shutdown(wait=wait, cancel_futures=True)
