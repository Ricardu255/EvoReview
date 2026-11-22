"""Batched review engine: lossless packing, metered budget pause/resume and
report confirmation.  Ported from the parallel EvoAgnet branch."""

import json
import os
import tempfile
import time
import unittest
from dataclasses import replace

from agentic_fake import FakeAgenticClient, enable_agentic_service
from evoagent.agent.batch_review import BatchReview, ReviewConflict
from evoagent.agent.review_batches import parse_changes, plan_batches, token_bound
from evoagent.config import Settings
from evoagent.service import ReviewService


DIFF = "--- a/a.py\n+++ b/a.py\n@@ -5 +5 @@\n-old_value\n+new_value\n"


class ReviewBatchTests(unittest.TestCase):
    def test_large_lines_and_deletions_are_preserved(self):
        old = "old_" * 1000
        new = "ä中x" * 2000
        diff = (f"--- a/previous name.txt\n+++ b/new name.txt\n@@ -10 +28 @@\n-{old}\n+{new}\n"
                "--- a/removed.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-removed content\n"
                "diff --git a/picture.png b/picture.png\nBinary files a/picture.png and b/picture.png differ\n")
        batches, excluded = plan_batches(diff, 410)
        changed = [item for batch in batches for item in parse_changes(batch["diff"])[0]]
        self.assertTrue(all(token_bound(batch["diff"]) <= 410 for batch in batches))
        self.assertEqual(old, "".join(item.text for item in changed if item.side == "-" and item.line == 10))
        self.assertEqual(new, "".join(item.text for item in changed if item.side == "+" and item.line == 28))
        self.assertEqual(["removed content"], [item.text for item in changed if item.old_path == "removed.txt"])
        self.assertEqual([{"path": "picture.png", "kind": "binary"}], excluded)

    def test_inconsistent_hunk_cannot_be_marked_covered(self):
        with self.assertRaisesRegex(ValueError, "incomplete"):
            plan_batches("--- a/a\n+++ b/a\n@@ -1,2 +1,2 @@\n-old\n+new\n", 410)

    def test_adaptive_split_preserves_original_long_line_offsets(self):
        content = "0123456789" * 1000
        batches, _ = plan_batches(f"--- a/a\n+++ b/a\n@@ -2,0 +3 @@\n+{content}\n", 800)
        plan = {"batches": batches}
        parent = batches[1]
        original_offset = parent["ranges"][0]["offset"]
        self.assertGreater(original_offset, 0)
        last_id = max(batch["id"] for batch in batches)
        self.assertTrue(BatchReview._split_batch(plan, 1))
        children = [batch for batch in plan["batches"] if batch["id"] > last_id]
        self.assertEqual(original_offset, children[0]["ranges"][0]["offset"])
        self.assertEqual(parse_changes(parent["diff"])[0][0].text, "".join(
            row.text for batch in children for row in parse_changes(batch["diff"])[0]
        ))


class BatchedServiceTests(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.settings = Settings(
            host="127.0.0.1", port=8080, db_path=self.path, max_diff_bytes=100000,
            max_steps=8, timeout_seconds=10, llm_base_url="", llm_api_key="", llm_model="",
            github_webhook_secret="", github_token="", auto_post_review=False,
        )

    def tearDown(self):
        if hasattr(self, "service"):
            self.service.queue.close()
        os.unlink(self.path)

    def create_service(self, **options):
        self.service = enable_agentic_service(
            ReviewService(replace(self.settings, **options))
        )
        return self.service

    def test_budget_pause_resumes_only_unfinished_and_confirmation_is_idempotent(self):
        # 7000 lets the first batch (overview + delegate + two workers +
        # critic + finalize, ~300 settled tokens per call) complete and pauses
        # inside batch 2's security worker, whose request estimate is the
        # largest of the cycle.
        service = self.create_service(review_task_token_budget=7000)
        calls = []

        class Client(FakeAgenticClient):
            def complete_json(self, role, system, user, ledger=None, max_tokens=None):
                task = json.loads(json.loads(user)["task"])
                calls.append((role, task.get("phase"), task.get("change_overview", {}).get("batch_id")))
                ledger.record_model(
                    role, self.provider, self.model,
                    {"input_tokens": 250, "output_tokens": 50}, 1,
                )
                return super().complete_json(role, system, user, None, max_tokens=max_tokens)

        service.chat_client = Client()
        diff = "".join(
            f"--- a/file{i}.txt\n+++ b/file{i}.txt\n@@ -1 +1 @@\n-old\n+{'abcde' * 600}\n"
            for i in range(12)
        )
        result = service.create_review("org/repo", diff)
        task_id = result["task_id"]
        self.assertEqual("PAUSED_BUDGET", result["state"])
        paused = service.review_details(task_id)
        complete_ids = [b["id"] for b in paused["coverage"]["batches"] if b["status"] == "completed"]
        self.assertTrue(complete_ids)
        self.assertLess(len(complete_ids), paused["coverage"]["total"])
        self.assertFalse(paused["partial_report"]["execution"]["coverage"]["complete"])
        with self.assertRaises(ReviewConflict):
            service.accept_review(task_id, "wrong-revision", "default", "reviewer")
        before_resume = len(calls)
        service.resume_task(task_id, "default", 40000)
        deadline = time.monotonic() + 10
        task = service.store.get(task_id)
        while task["state"] not in {"FAILED", "SUCCESS"} and time.monotonic() < deadline:
            time.sleep(.03)
            task = service.store.get(task_id)
        self.assertEqual("SUCCESS", task["state"], task["error"])
        self.assertTrue(task["report"]["execution"]["coverage"]["complete"])
        self.assertFalse(set(complete_ids) & {bid for _, _, bid in calls[before_resume:]})
        self.assertFalse(any(phase == "overview" for _, phase, _ in calls[before_resume:]))
        detail = service.review_details(task_id)
        accepted = service.accept_review(task_id, detail["report_revision"], "default", "reviewer")
        self.assertEqual(
            accepted,
            service.accept_review(task_id, detail["report_revision"], "default", "second"),
        )
        self.assertEqual("reviewer", accepted["actor"])
        with self.assertRaisesRegex(ValueError, "task not found"):
            service.accept_review(task_id, detail["report_revision"], "other", "second")

    def test_invalid_action_is_corrected_once_without_changing_coverage(self):
        service = self.create_service()
        corrections = []

        class Client(FakeAgenticClient):
            def complete_json(self, role, system, user, ledger=None, max_tokens=None):
                task = json.loads(json.loads(user)["task"])
                if task.get("phase") == "delegate":
                    corrections.append(system)
                    if len(corrections) == 1:
                        ledger.record_model(
                            role, self.provider, self.model,
                            {"input_tokens": 80, "output_tokens": 10}, 1,
                        )
                        return {"delegations": []}
                return super().complete_json(role, system, user, ledger, max_tokens=max_tokens)

        service.chat_client = Client()
        result = service.create_review("org/repo", DIFF)
        self.assertEqual(2, len(corrections))
        self.assertIn("required complete JSON object", corrections[1])
        self.assertTrue(result["report"]["execution"]["coverage"]["complete"])


if __name__ == "__main__":
    unittest.main()
