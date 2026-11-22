"""Batched-engine lifecycle: engine pinning, resume top-ups, startup recovery."""

import os
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch

from agentic_fake import enable_agentic_service
from evoagent.config import Settings
from evoagent.domain.models import TaskState
from evoagent.service import ReviewService


DIFF = "--- a/a.py\n+++ b/a.py\n@@ -5 +5 @@\n-old_value\n+new_value\n"


def settings(path, **options):
    return Settings(
        host="127.0.0.1", port=8080, db_path=path, max_diff_bytes=100000,
        max_steps=8, timeout_seconds=10, llm_base_url="", llm_api_key="", llm_model="",
        github_webhook_secret="", github_token="", auto_post_review=False,
        **options,
    )


class BatchedLifecycleTests(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.service = enable_agentic_service(ReviewService(settings(self.path)))

    def tearDown(self):
        self.service.queue.close()
        os.unlink(self.path)

    def test_engine_is_pinned_at_creation_and_respected_on_dispatch(self):
        task_id = self.service._create_task("org/a", DIFF, 1, "test", "tenant-a")
        self.assertEqual(
            "batched-v1",
            self.service.store.get(task_id)["input"]["review_engine"],
        )
        legacy = self.service._create_task("org/a", DIFF, 1, "test", "tenant-a")
        self.service.store.update_task_input(legacy, {"review_engine": ""})
        # A task pinned to the legacy engine keeps it even though the global
        # switch is still on: dispatch reads the stored engine, not settings.
        with patch.object(
            self.service._reviews_app.execution, "batch_runner",
        ) as runner:
            self.service._run_review(legacy, "org/a", 1, DIFF, "tenant-a")
            self.assertFalse(runner.called)

    def test_flag_off_creates_legacy_tasks(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        try:
            service = enable_agentic_service(ReviewService(
                settings(path, review_batching_enabled=False),
            ))
            task_id = service._create_task("org/a", DIFF, 1, "test", "tenant-a")
            self.assertNotIn(
                "review_engine", service.store.get(task_id)["input"],
            )
            service.queue.close()
        finally:
            os.unlink(path)

    def test_budget_paused_resume_requires_top_up_and_records_it(self):
        from evoagent.time_utils import utc_now
        from evoagent.domain.models import TraceEvent

        task_id = self.service._create_task("org/a", DIFF, 1, "test", "tenant-a")
        self.service.store.transition(task_id, TraceEvent(
            1, TaskState.PAUSED_BUDGET, "budget paused", utc_now(),
        ))
        with self.assertRaisesRegex(ValueError, "additional_tokens is required"):
            self.service.resume_task(task_id, "tenant-a")
        with patch.object(self.service.queue, "submit") as submit:
            result = self.service.resume_task(task_id, "tenant-a", 5000)
            self.assertTrue(result["resumed"])
            self.assertEqual(
                5000,
                self.service.store.get(task_id)["input"]["additional_budget_tokens"],
            )
            self.assertEqual("PENDING", self.service.store.get(task_id)["state"])
            self.assertEqual(task_id, submit.call_args[0][0]["task_id"])

    def test_resume_rejects_invalid_top_ups(self):
        from evoagent.time_utils import utc_now
        from evoagent.domain.models import TraceEvent

        task_id = self.service._create_task("org/a", DIFF, 1, "test", "tenant-a")
        self.service.store.transition(task_id, TraceEvent(
            1, TaskState.PAUSED_BUDGET, "budget paused", utc_now(),
        ))
        with self.assertRaisesRegex(ValueError, "between 0 and 1000000"):
            self.service.resume_task(task_id, "tenant-a", 1000001)
        with self.assertRaisesRegex(ValueError, "between 0 and 1000000"):
            self.service.resume_task(task_id, "tenant-a", -5)

    def test_prepare_resume_accepts_paused_budget(self):
        task_id = self.service._create_task("org/a", DIFF, 1, "test", "tenant-a")
        store = self.service.store
        store.update_task_input(task_id, {"review_engine": "batched-v1"})
        from evoagent.time_utils import utc_now
        store.transition(task_id, __import__("evoagent.domain.models", fromlist=["TraceEvent"]).TraceEvent(
            1, TaskState.PAUSED_BUDGET, "budget paused", utc_now(),
        ))
        self.assertEqual("PAUSED_BUDGET", store.get(task_id)["state"])
        self.assertTrue(store.prepare_resume(task_id, "tenant-a"))
        self.assertEqual("PENDING", store.get(task_id)["state"])

    def test_interrupted_tasks_lists_only_batched_running_tasks(self):
        store = self.service.store
        from evoagent.time_utils import utc_now
        from evoagent.domain.models import TraceEvent

        batched = self.service._create_task("org/a", DIFF, 1, "test", "tenant-a")
        store.transition(batched, TraceEvent(1, TaskState.EXECUTING, "working", utc_now()))
        paused = self.service._create_task("org/a", DIFF, 2, "test", "tenant-a")
        store.transition(paused, TraceEvent(1, TaskState.PAUSED_BUDGET, "paused", utc_now()))
        done = self.service._create_task("org/a", DIFF, 3, "test", "tenant-a")
        store.transition(done, TraceEvent(1, TaskState.SUCCESS, "done", utc_now()))
        # Budget-paused tasks need an explicit top-up, not a blind redelivery.
        self.assertEqual([batched], store.interrupted_tasks())

    def test_startup_recovery_resubmits_interrupted_tasks(self):
        store = self.service.store
        from evoagent.time_utils import utc_now
        from evoagent.domain.models import TraceEvent

        stuck = self.service._create_task("org/a", DIFF, 1, "test", "tenant-a")
        store.transition(stuck, TraceEvent(1, TaskState.EXECUTING, "working", utc_now()))
        submitted = []
        with patch.object(
            self.service.queue, "submit",
            side_effect=lambda payload, message_id=None: submitted.append(payload["task_id"]),
        ), patch("evoagent.service.Timer"):
            self.service._recover_local_tasks()
        self.assertEqual([stuck], submitted)


if __name__ == "__main__":
    unittest.main()
