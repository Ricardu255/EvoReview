"""Tests for automatic repair delivery."""
import os
import tempfile
import unittest

from evoagent.config import Settings
from evoagent.domain.models import ReviewReport, TaskState, TraceEvent
from evoagent.service import ReviewService
from evoagent.store import utc_now


class FakeFixer:
    def __init__(self):
        self.calls = []

    def create_fix_commits(self, client, repository, pull_request, report):
        self.calls.append((client, repository, pull_request, report))
        return {"repository": repository, "pull_request": pull_request}


class FixesApplicationServiceTests(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.settings = Settings(
            host="127.0.0.1", port=8080, db_path=self.path, max_diff_bytes=10000,
            max_steps=8, timeout_seconds=10, llm_base_url="", llm_api_key="",
            llm_model="", github_webhook_secret="", github_token="",
            auto_post_review=False,
        )
        self.service = ReviewService(self.settings)
        self.addCleanup(self.service.queue.close)
        self.fixer = FakeFixer()
        self.client = object()
        self.service._fixes_app.fixer = self.fixer
        self.service._fixes_app.github_client_for_installation = (
            lambda installation_id: self.client
        )

    def tearDown(self):
        os.unlink(self.path)

    def _completed_task(self, repository="org/a", pull_request=1, tenant="tenant-a"):
        task_id = "fix-%s" % repository.replace("/", "-")
        self.service.store.create(task_id, repository, pull_request, {}, tenant)
        self.service.store.succeed(task_id, ReviewReport(
            repository=repository, pull_request=pull_request,
            summary="reviewed", risk="low",
        ), TraceEvent(1, TaskState.SUCCESS, "done", utc_now()))
        return task_id

    def test_create_fix_delegates_to_configured_fixer(self):
        task_id = self._completed_task()
        self.service.store.grant_repository("tenant-a", "org/a", auto_fix=True)

        result = self.service.create_fix(task_id, tenant_id="tenant-a")

        self.assertEqual({"repository": "org/a", "pull_request": 1}, result)
        self.assertEqual(1, len(self.fixer.calls))
        client, repository, pull_request, report = self.fixer.calls[0]
        self.assertIs(self.client, client)
        self.assertEqual("org/a", repository)
        self.assertEqual(1, pull_request)
        self.assertEqual("reviewed", report["summary"])

    def test_create_fix_requires_auto_fix_grant(self):
        task_id = self._completed_task(repository="org/b")
        self.service.store.grant_repository("tenant-a", "org/b", auto_fix=False)

        with self.assertRaises(PermissionError):
            self.service.create_fix(task_id, tenant_id="tenant-a")
        self.assertEqual([], self.fixer.calls)

    def test_create_fix_requires_completed_pr_task(self):
        self.service.store.create("pending", "org/a", 1, {}, "tenant-a")
        with self.assertRaisesRegex(ValueError, "completed task"):
            self.service.create_fix("pending", tenant_id="tenant-a")

        task_id = self._completed_task(pull_request=None)
        self.service.store.grant_repository("tenant-a", "org/a", auto_fix=True)
        with self.assertRaisesRegex(ValueError, "pull request"):
            self.service.create_fix(task_id, tenant_id="tenant-a")


if __name__ == "__main__":
    unittest.main()
