"""Behavioral regressions for isolation, resumability and actual release execution."""
from dataclasses import replace
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
import urllib.parse
import uuid
from unittest.mock import patch

from agentic_fake import FakeAgenticClient, enable_agentic_service
from evoagent.api import ApiHandler, body_optional
from evoagent.adapters.observability import AlertManager
from evoagent.app.comments import review_comment_marker
from evoagent.errors import ModelNotConfigured
from evoagent.adapters.auth import Principal
from evoagent.config import Settings
from evoagent.domain.diff_parser import parse_unified_diff
from evoagent.domain.models import ReviewReport, TaskState, TraceEvent
from evoagent.errors import PermanentTaskError
from evoagent.agent.repository_tools import RepositoryToolSuite
from evoagent.agent.runtime import RuntimeCancelled
from evoagent.service import ReviewService
from evoagent.adapters.stores.sqlite.store import TaskStore
from evoagent.time_utils import utc_now


DIFF = "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+eval(data)\n"
CLEAN = "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+value = 1\n"


class RecordingClient(FakeAgenticClient):
    def __init__(self):
        self.prompts = []
        self.fail_marker = ""

    def complete_json(self, role, system, user, ledger=None, max_tokens=None):
        self.prompts.append(system)
        if self.fail_marker and self.fail_marker in system:
            raise RuntimeError("controlled candidate failure")
        return super().complete_json(role, system, user, ledger, max_tokens)


class ReviewRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(
            host="127.0.0.1", port=8080, db_path=str(Path(self.temp.name) / "test.db"),
            max_diff_bytes=10000, max_steps=8, timeout_seconds=10,
            llm_base_url="", llm_api_key="", llm_model="", github_webhook_secret="",
            github_token="", auto_post_review=False,
        )
        self.service = enable_agentic_service(ReviewService(self.settings))
        self.addCleanup(self.service.queue.close)
        self.client = RecordingClient()
        self.service.chat_client = self.client

    def request(self, path, method="GET", payload=None, tenant="tenant-a"):
        handler = ApiHandler.__new__(ApiHandler)
        handler.service, handler.settings, handler.path = self.service, self.settings, path
        handler._principal = lambda permission="read": Principal("alice", "alice", tenant, "admin")
        handler._read_body = lambda: json.dumps(payload or {}).encode("utf-8")
        result = {}
        handler._send_json = lambda code, body: result.update(status=code, body=body)
        getattr(handler, "do_" + method)()
        return result

    def versions(self, tenant="tenant-a"):
        store = self.service.store
        stable = store.save_skill_version("llm-review", "STABLE_MARKER", .5, True, tenant)
        candidate = store.save_skill_version("llm-review", "CANDIDATE_MARKER", .6, False, tenant)
        return stable["version"], candidate["version"]

    def configure(self, canary=0, shadow=0, **options):
        stable, candidate = self.versions()
        self.service.releases.configure("tenant-a", "llm-review", {
            "stable_version": stable, "candidate_version": candidate,
            "canary_percent": canary, "shadow_percent": shadow, **options,
        })
        return stable, candidate

    def test_repository_grant_api_lifecycle(self):
        created = self.request("/v1/repository-grants", "POST", {
            "repository": "org/a", "auto_fix": True,
        })
        self.assertEqual(201, created["status"])
        self.assertEqual(
            [{"repository": "org/a", "auto_fix": True}],
            self.request("/v1/repository-grants")["body"]["grants"],
        )
        deleted = self.request(
            "/v1/repository-grants?repository=org/a", "DELETE"
        )
        self.assertEqual(200, deleted["status"])
        self.assertTrue(deleted["body"]["revoked"])
        self.assertEqual(
            [], self.request("/v1/repository-grants")["body"]["grants"]
        )

    def test_repository_grant_api_validates_input(self):
        self.assertEqual(
            400,
            self.request("/v1/repository-grants", "POST", {
                "repository": "",
            })["status"],
        )
        self.assertEqual(
            400,
            self.request("/v1/repository-grants", "POST", {
                "repository": "org/a", "auto_fix": "yes",
            })["status"],
        )


    def test_api_rejects_unbound_checkout_and_uses_server_mapping(self):
        authorized = Path(self.temp.name) / "allowed"
        foreign = Path(self.temp.name) / "foreign"
        authorized.mkdir()
        foreign.mkdir()
        self.service.store.grant_repository("tenant-a", "org/a")
        self.service.repository_roots = {"tenant-a": {"org/a": str(authorized)}}
        result = self.request("/v1/reviews", "POST", {
            "repository": "org/a", "diff": DIFF, "repository_root": str(foreign),
        })
        self.assertEqual(403, result["status"])
        self.assertEqual([], self.service.store.list_tasks())
        created = self.service.create_review("org/a", DIFF, tenant_id="tenant-a")
        task = self.service.store.get(created["task_id"], "tenant-a")
        self.assertEqual(os.path.realpath(authorized), task["input"]["repository_root"])
        with self.assertRaises(PermissionError):
            self.service.create_review("org/a", DIFF, tenant_id="tenant-b", repository_root=str(authorized))

    def test_repository_name_cannot_enable_implicit_local_reading(self):
        result = self.service.create_review(self.temp.name, DIFF)
        self.assertFalse(result["report"]["execution"]["repository_context"]["available"])

    def test_tools_reject_traversal_and_symlink_escape(self):
        checkout = Path(self.temp.name) / "checkout"
        checkout.mkdir()
        outside = Path(self.temp.name) / "outside.txt"
        outside.write_text("controlled marker", encoding="utf-8")
        suite = RepositoryToolSuite(str(checkout), DIFF, parse_unified_diff(DIFF))
        with self.assertRaises(ValueError):
            suite.read_file("../outside.txt")
        try:
            (checkout / "link.txt").symlink_to(outside)
        except OSError:
            # Windows may deny symlink creation; still verify realpath containment.
            with patch("evoagent.agent.repository_tools.os.path.realpath", return_value=str(outside)):
                with self.assertRaises(ValueError):
                    suite.read_file("link.txt")
        else:
            with self.assertRaises(ValueError):
                suite.read_file("link.txt")

    def test_dead_letters_are_filtered_and_foreign_replay_is_rejected(self):
        queue = self.service.queue
        for tenant in ("tenant-a", "tenant-b"):
            queue._dead_letter({"message_id": tenant, "payload": {"tenant_id": tenant}}, "controlled")
        response = self.request("/api/queue/dead-letters")
        self.assertEqual(["tenant-a"], [item["message_id"] for item in response["body"]["messages"]])
        with patch.object(queue, "submit") as submit:
            self.assertEqual(404, self.request("/v1/queue/dead-letters/replay", "POST", {"message_id": "tenant-b"})["status"])
            submit.assert_not_called()
            self.assertEqual(202, self.request("/v1/queue/dead-letters/replay", "POST", {"message_id": "tenant-a"})["status"])
            self.assertEqual("tenant-a", submit.call_args[0][0]["tenant_id"])

    def test_prompt_activation_and_execution_are_tenant_scoped(self):
        store = self.service.store
        store.save_skill_version("llm-review", "TENANT_A_MARKER", 1, True, "tenant-a")
        store.save_skill_version("llm-review", "TENANT_B_MARKER", 1, True, "tenant-b")
        store.save_skill_version("llm-review", "B_ONLY_CANDIDATE", 1, False, "tenant-b")
        denied = self.request("/v1/skills/llm-review/versions/2/activate", "POST")
        self.assertEqual(404, denied["status"])
        self.service.create_review("org/a", DIFF, tenant_id="tenant-a")
        self.assertTrue(all("TENANT_A_MARKER" in prompt for prompt in self.client.prompts))
        self.assertFalse(any("TENANT_B_MARKER" in prompt for prompt in self.client.prompts))
        self.assertEqual("TENANT_B_MARKER", store.get_active_skill_version("llm-review", "tenant-b")["prompt"])

    def test_evaluation_cases_and_evolution_records_are_tenant_scoped(self):
        store = self.service.store
        store.save_evaluation_case("same-name", "validation", DIFF, [], tenant_id="tenant-a")
        store.save_evaluation_case("same-name", "validation", CLEAN, [], tenant_id="tenant-b")
        run = {"id": "foreign-run", "tenant_id": "tenant-b", "skill_name": "llm-review",
               "candidate_version": 1, "decision": "deferred", "candidate_score": 0,
               "baseline_score": 0, "metrics": {"note": "private"}, "created_at": utc_now()}
        store.save_evolution_run(run)
        self.assertEqual([], self.request("/v1/evolution/runs")["body"]["runs"])
        cases = self.request("/v1/evaluation/cases")["body"]["cases"]
        self.assertEqual([DIFF], [item["diff"] for item in cases])
        self.assertFalse(store.update_evolution_run("foreign-run", "activated", {}, "tenant-a"))
        self.assertEqual("deferred", store.list_evolution_runs(50, "tenant-b")[0]["decision"])

    def test_prompt_proposal_api_writes_only_calling_tenant(self):
        response = self.request("/v1/evolution/propose", "POST", {
            "skill_name": "llm-review", "prompt": "Review diff severity fix test JSON",
        })
        self.assertEqual(201, response["status"])
        self.assertEqual(1, len(self.service.store.list_skill_versions("llm-review", "tenant-a")))
        self.assertEqual([], self.service.store.list_skill_versions("llm-review", "tenant-b"))
        self.assertEqual([], self.service.store.list_skill_versions("llm-review"))

    def test_cancelled_task_can_resume_and_running_task_cannot(self):
        service = self.service
        task_id = service._create_task("org/a", DIFF, 1, "test", "tenant-a")
        service.cancel_task(task_id, "tenant-a")
        with self.assertRaises(RuntimeCancelled):
            service._run_review(task_id, "org/a", 1, DIFF, "tenant-a")
        self.assertEqual("CANCELLED", service.store.get(task_id)["state"])
        with patch.object(service.queue, "submit") as submit:
            self.assertTrue(service.resume_task(task_id, "tenant-a")["resumed"])
            self.assertFalse(service.store.is_cancelled(task_id))
            service._process_queued(submit.call_args[0][0])
        self.assertEqual("SUCCESS", service.store.get(task_id)["state"])
        pending = service._create_task("org/a", DIFF, None, "test", "tenant-a")
        with self.assertRaisesRegex(ValueError, "only failed or cancelled"):
            service.resume_task(pending, "tenant-a")

    def webhook(self):
        return {"action": "opened", "number": 12, "repository": {"full_name": "org/a"},
                "pull_request": {"diff_url": "https://github.com/org/a/pull/12.diff"}}

    def test_webhook_rejects_unbound_installation(self):
        payload = dict(self.webhook(), installation={"id": 99})
        with self.assertRaisesRegex(PermissionError, "not bound"):
            self.service.handle_github_pull_request(payload, "unbound", "digest")
        self.assertEqual([], self.service.store.list_tasks())

        self.service.store.save_installation(99, "account", "tenant-a")
        with patch.object(self.service.queue, "submit"):
            result = self.service.handle_github_pull_request(
                payload, "bound", "digest",
            )
        task = self.service.store.get(result["task_id"], "tenant-a")
        self.assertEqual("tenant-a", task["tenant_id"])

    def test_webhook_queue_failure_retries_same_task(self):
        service = self.service
        with patch.object(service.queue, "submit", side_effect=[RuntimeError("offline"), "queued"]) as submit:
            with self.assertRaisesRegex(RuntimeError, "offline"):
                service.handle_github_pull_request(self.webhook(), "delivery", "digest", "tenant-a")
            first_task = service.store.get_webhook("delivery")["task_id"]
            self.assertEqual("pending", service.store.get_webhook("delivery")["status"])
            retry = service.handle_github_pull_request(self.webhook(), "delivery", "digest", "tenant-a")
            duplicate = service.handle_github_pull_request(self.webhook(), "delivery", "digest", "tenant-a")
            self.assertEqual(2, submit.call_count)
            self.assertEqual(first_task, retry["task_id"])
            self.assertTrue(duplicate["duplicate"])
            self.assertEqual(1, len(service.store.list_tasks()))

    def test_webhook_retry_recovers_crash_before_task_binding(self):
        service = self.service
        with patch.object(service.store, "bind_webhook_task", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                service.handle_github_pull_request(self.webhook(), "delivery", "digest", "tenant-a")
        with patch.object(service.queue, "submit"):
            service.handle_github_pull_request(self.webhook(), "delivery", "digest", "tenant-a")
        self.assertEqual(1, len(service.store.list_tasks()))

    def test_process_queued_records_failure_and_releases_observation(self):
        service = self.service
        task_id = service._create_task("org/a", DIFF, 1, "test", "tenant-a")

        class BrokenClient:
            provider = "broken"
            model = "broken"

            def complete_json(self, *args, **kwargs):
                raise RuntimeError("model offline")

        service.chat_client = BrokenClient()
        observations = []
        with patch.object(
            service.releases, "observe",
            side_effect=lambda *args: observations.append(args),
        ), patch.object(service.alerts, "evaluate") as evaluate:
            with self.assertRaisesRegex(RuntimeError, "model offline"):
                service._process_queued({
                    "task_id": task_id, "repository": "org/a",
                    "pull_request": 1, "tenant_id": "tenant-a",
                })
        evaluate.assert_called_once_with("tenant-a")
        self.assertTrue(observations)
        self.assertTrue(observations[-1][2])

    def test_process_queued_rejects_missing_mismatched_and_payload_less_tasks(self):
        service = self.service
        with self.assertRaises(PermanentTaskError):
            service._process_queued({
                "task_id": "missing", "repository": "org/a", "tenant_id": "tenant-a",
            })

        task_id = service._create_task("org/a", DIFF, 1, "test", "tenant-a")
        with self.assertRaises(PermanentTaskError):
            service._process_queued({
                "task_id": task_id, "repository": "org/a", "tenant_id": "tenant-b",
            })
        with self.assertRaises(PermanentTaskError):
            service._process_queued({
                "task_id": task_id, "repository": "org/b", "tenant_id": "tenant-a",
            })

        deferred = service._create_deferred_task(
            "org/a", 1, "github-webhook", "tenant-a", {},
        )
        with self.assertRaises(PermanentTaskError):
            service._process_queued({
                "task_id": deferred, "repository": "org/a",
                "tenant_id": "tenant-a",
            })

    def test_deferred_webhook_task_fetches_diff_and_posts_comment(self):
        service = self.service

        class FakeGitHub:
            def __init__(self):
                self.comments = []

            def ensure_repository_access(self, repository):
                self.repository = repository

            def fetch_diff(self, url):
                self.diff_url = url
                return DIFF

            def upsert_comment(self, issue_url, body, marker):
                self.comments.append((issue_url, body, marker))

        github = FakeGitHub()
        task_id = service._create_deferred_task(
            "org/a", 1, "github-webhook", "tenant-a",
            {"diff_url": "https://github.com/org/a/pull/1.diff"},
        )
        self.assertIsNone(service.store.get_task_payload(task_id))
        payload = {
            "task_id": task_id, "repository": "org/a", "pull_request": 1,
            "tenant_id": "tenant-a", "installation_id": 42,
            "diff_url": "https://github.com/org/a/pull/1.diff",
            "github_issue_url": "https://github.com/org/a/pull/1",
        }
        original_settings = service._reviews_app.execution.settings
        service._reviews_app.execution.settings = replace(
            original_settings, auto_post_review=True,
        )
        try:
            with patch.object(
                service._github_app, "github_client_for_installation",
                return_value=github,
            ):
                service._process_queued(payload)
        finally:
            service._reviews_app.execution.settings = original_settings

        task = service.store.get(task_id, "tenant-a")
        self.assertEqual("SUCCESS", task["state"])
        self.assertEqual(DIFF, service.store.get_task_payload(task_id))
        self.assertFalse(task["input"]["diff_pending"])
        self.assertEqual(["org/a"], [github.repository])
        self.assertEqual(1, len(github.comments))
        self.assertEqual(
            review_comment_marker("tenant-a", "https://github.com/org/a/pull/1", task_id),
            github.comments[0][2],
        )

    def test_comment_delivery_failure_does_not_fail_completed_review(self):
        service = self.service

        class FailingCommentClient:
            def ensure_repository_access(self, repository):
                pass

            def fetch_diff(self, url):
                return DIFF

            def upsert_comment(self, issue_url, body, marker):
                raise RuntimeError("github unavailable")

        task_id = service._create_deferred_task(
            "org/a", 1, "github-webhook", "tenant-a",
            {"diff_url": "https://github.com/org/a/pull/1.diff"},
        )
        payload = {
            "task_id": task_id, "repository": "org/a", "pull_request": 1,
            "tenant_id": "tenant-a", "installation_id": 42,
            "diff_url": "https://github.com/org/a/pull/1.diff",
            "github_issue_url": "https://github.com/org/a/pull/1",
        }
        original_settings = service._reviews_app.execution.settings
        service._reviews_app.execution.settings = replace(
            original_settings, auto_post_review=True,
        )
        service._reviews_app.execution.comments.BACKOFF_SECONDS = 0
        try:
            with patch.object(
                service._github_app, "github_client_for_installation",
                return_value=FailingCommentClient(),
            ):
                # A comment outage must not turn a completed review into a
                # failed (and therefore retried) task.
                service._process_queued(payload)
        finally:
            service._reviews_app.execution.settings = original_settings

        self.assertEqual("SUCCESS", service.store.get(task_id, "tenant-a")["state"])

    def test_resume_payload_keeps_github_delivery_metadata(self):
        service = self.service
        with patch.object(service.queue, "submit") as submit:
            result = service.enqueue_review(
                "org/a", DIFF, 1, tenant_id="tenant-a",
                github_issue_url="https://github.com/org/a/pull/1",
                installation_id=42,
            )
        task_id = result["task_id"]
        task = service.store.get(task_id, "tenant-a")
        self.assertEqual(42, task["input"]["installation_id"])
        self.assertEqual(
            "https://github.com/org/a/pull/1", task["input"]["github_issue_url"]
        )
        service.store.fail(
            task_id, "boom", TraceEvent(1, TaskState.FAILED, "boom", utc_now()),
        )
        with patch.object(service.queue, "submit") as submit:
            self.assertTrue(service.resume_task(task_id, "tenant-a")["resumed"])
        payload = submit.call_args[0][0]
        self.assertEqual(42, payload["installation_id"])
        self.assertEqual(
            "https://github.com/org/a/pull/1", payload["github_issue_url"]
        )

    def test_canary_executes_pinned_candidate_after_configuration_changes(self):
        stable, candidate = self.configure(canary=100)
        task_id = self.service._create_task("org/a", DIFF, 1, "test", "tenant-a")
        self.service.store.save_skill_version("llm-review", "NEW_ACTIVE_MARKER", .8, True, "tenant-a")
        self.service.store.save_deployment("tenant-a", "llm-review", {
            "stable_version": stable, "candidate_version": 3, "canary_percent": 0,
        })
        report = self.service._run_review(task_id, "org/a", 1, DIFF, "tenant-a")
        self.assertEqual(candidate, report.execution["prompt_version"])
        self.assertEqual(5, len(self.client.prompts))
        self.assertTrue(all("CANDIDATE_MARKER" in prompt for prompt in self.client.prompts))

    def test_shadow_executes_separate_candidate_without_publishing_it(self):
        stable, candidate = self.configure(shadow=100)
        result = self.service.create_review("org/a", DIFF, tenant_id="tenant-a")
        self.assertEqual(stable, result["report"]["execution"]["prompt_version"])
        self.assertEqual(10, len(self.client.prompts))
        self.assertEqual(5, sum("STABLE_MARKER" in prompt for prompt in self.client.prompts))
        self.assertEqual(5, sum("CANDIDATE_MARKER" in prompt for prompt in self.client.prompts))
        shadow_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "evoagent:shadow:" + result["task_id"]))
        self.assertEqual(candidate, self.service.store.get(shadow_id)["report"]["execution"]["prompt_version"])
        observations = self.service.store.list_release_observations("tenant-a", "llm-review")
        self.assertEqual(1, len(observations))
        self.assertTrue(self.service.store.load_checkpoints(shadow_id))
        memories = self.service.store.list_agent_memories("tenant-a", "org/a", ("episodic", "working"))
        self.assertFalse(any(item["task_id"] == shadow_id for item in memories))

    def test_candidate_failure_rolls_back_execution_to_stable(self):
        stable, _ = self.configure(canary=100, min_samples=1, max_error_rate=.1)
        self.client.fail_marker = "CANDIDATE_MARKER"
        with self.assertRaises(RuntimeError):
            self.service.create_review("org/a", DIFF, tenant_id="tenant-a")
        self.assertEqual("rolled_back", self.service.store.get_deployment("tenant-a", "llm-review")["status"])
        self.client.fail_marker = ""
        self.client.prompts.clear()
        result = self.service.create_review("org/a", DIFF, tenant_id="tenant-a")
        self.assertEqual(stable, result["report"]["execution"]["prompt_version"])
        self.assertTrue(all("STABLE_MARKER" in prompt for prompt in self.client.prompts))

    def test_shadow_failure_preserves_successful_primary(self):
        stable, _ = self.configure(shadow=100)
        self.client.fail_marker = "CANDIDATE_MARKER"
        result = self.service.create_review("org/a", DIFF, tenant_id="tenant-a")
        self.assertEqual("SUCCESS", result["state"])
        self.assertEqual(stable, result["report"]["execution"]["prompt_version"])
        observations = self.service.store.list_release_observations("tenant-a", "llm-review")
        self.assertTrue(observations[0]["candidate_failed"])

    def test_shadow_observation_failure_does_not_fail_primary_and_retry_is_idempotent(self):
        self.configure(shadow=100)
        real_observe = self.service.releases.observe_shadow

        def interrupted(*args, **kwargs):
            real_observe(*args, **kwargs)
            raise RuntimeError("crash after persisting observation")

        with patch.object(self.service.releases, "observe_shadow", side_effect=interrupted):
            result = self.service.create_review("org/a", DIFF, tenant_id="tenant-a")
        self.assertEqual("SUCCESS", result["state"])
        self.service._run_review(result["task_id"], "org/a", None, DIFF, "tenant-a")
        self.assertEqual(1, len(self.service.store.list_release_observations("tenant-a", "llm-review")))
        self.assertEqual(1, self.service.store.get_deployment("tenant-a", "llm-review")["shadow_samples"])

    def test_resume_queue_failure_remains_retryable(self):
        task_id = self.service._create_task("org/a", DIFF, 1, "test", "tenant-a")
        self.service.cancel_task(task_id, "tenant-a")
        with self.assertRaises(RuntimeCancelled):
            self.service._run_review(task_id, "org/a", 1, DIFF, "tenant-a")
        with patch.object(self.service.queue, "submit", side_effect=RuntimeError("offline")):
            with self.assertRaisesRegex(RuntimeError, "offline"):
                self.service.resume_task(task_id, "tenant-a")
        self.assertEqual("FAILED", self.service.store.get(task_id)["state"])
        with patch.object(self.service.queue, "submit"):
            self.assertTrue(self.service.resume_task(task_id, "tenant-a")["resumed"])


class BodylessPostRouteTests(unittest.TestCase):
    def test_action_routes_do_not_require_a_json_body(self):
        for path in (
            "/v1/tasks/abc/comment", "/v1/tasks/abc/cancel",
            "/v1/tasks/abc/resume", "/v1/skills/reload",
        ):
            self.assertTrue(body_optional(path), path)

    def test_json_routes_still_require_a_body(self):
        for path in (
            "/v1/reviews", "/v1/repository-grants", "/webhooks/github",
            "/v1/tasks/abc/fix",
        ):
            self.assertFalse(body_optional(path), path)


class MissingModelApiTests(unittest.TestCase):
    def test_review_without_a_configured_model_returns_503(self):
        class Service:
            def create_review(self, *args, **kwargs):
                raise ModelNotConfigured("agentic review requires a configured model")

        handler = ApiHandler.__new__(ApiHandler)
        handler.service = Service()
        handler.settings = None
        handler.path = "/v1/reviews"
        handler._principal = lambda permission="read": Principal(
            "alice", "alice", "tenant-a", "admin"
        )
        handler._read_body = lambda: b'{"repository":"org/a","diff":"x"}'
        result = {}
        handler._send_json = lambda code, body: result.update(status=code, body=body)
        handler.do_POST()
        self.assertEqual(503, result["status"])
        self.assertIn("model", result["body"]["error"])

    def test_null_mode_is_treated_as_absent(self):
        calls = []
        result = {}

        class Service:
            def create_review(self, *args, **kwargs):
                calls.append((args, kwargs))
                raise ModelNotConfigured("agentic review requires a configured model")

        handler = ApiHandler.__new__(ApiHandler)
        handler.service = Service()
        handler.settings = None
        handler.path = "/v1/reviews"
        handler._principal = lambda permission="read": Principal(
            "alice", "alice", "tenant-a", "admin"
        )
        handler._read_body = lambda: (
            b'{"repository":"org/a","diff":"x","mode":null,"repository_root":null}'
        )
        handler._send_json = lambda code, body: result.update(status=code, body=body)
        handler.do_POST()
        self.assertEqual(503, result["status"])
        self.assertEqual(1, len(calls))
        args, kwargs = calls[0]
        self.assertEqual(("org/a", "x", None), args)
        self.assertEqual("", kwargs["mode"])
        self.assertEqual("", kwargs["repository_root"])

    def test_null_fields_are_audited_as_absent(self):
        audits = []
        result = {}

        class Store:
            def audit(self, *args):
                audits.append(args)

        class Service:
            def __init__(self):
                self.store = Store()

            def create_review(self, *args, **kwargs):
                return {"task_id": "task-1"}

        handler = ApiHandler.__new__(ApiHandler)
        handler.service = Service()
        handler.settings = None
        handler.path = "/v1/reviews"
        handler._principal = lambda permission="read": Principal(
            "alice", "alice", "tenant-a", "admin"
        )
        handler._read_body = lambda: (
            b'{"repository":null,"diff":"x","mode":null,"repository_root":null}'
        )
        handler._send_json = lambda code, body: result.update(status=code, body=body)
        handler.do_POST()
        self.assertEqual(201, result["status"])
        self.assertEqual(1, len(audits))
        tenant_id, actor, action, resource, detail = audits[0]
        self.assertEqual("tenant-a", tenant_id)
        self.assertEqual("alice", actor)
        self.assertEqual("review.create", action)
        self.assertEqual("", resource)
        self.assertEqual("", detail["mode"])


class NullFieldApiTests(unittest.TestCase):
    """JSON null must not be coerced into the literal string "None"."""

    def post(self, path, payload, service):
        handler = ApiHandler.__new__(ApiHandler)
        handler.service = service
        handler.settings = None
        handler.path = path
        handler._principal = lambda permission="read": Principal(
            "alice", "alice", "tenant-a", "admin"
        )
        handler._read_body = lambda: json.dumps(payload).encode("utf-8")
        result = {}
        handler._send_json = lambda code, body: result.update(status=code, body=body)
        handler.do_POST()
        return result

    def test_repository_grant_rejects_null_repository(self):
        class Store:
            def __init__(self):
                self.granted = []

            def grant_repository(self, *args):
                self.granted.append(args)

            def audit(self, *args, **kwargs):
                pass

        class Service:
            def __init__(self):
                self.store = Store()

        service = Service()
        result = self.post(
            "/v1/repository-grants",
            {"repository": None, "auto_fix": True},
            service,
        )
        self.assertEqual(400, result["status"])
        self.assertEqual([], service.store.granted)

    def test_evaluation_case_treats_null_split_and_name_as_absent(self):
        calls = []

        class Evolution:
            def add_evaluation_case(self, *args):
                calls.append(args)
                return {"id": 1}

        class Service:
            def evolution_for(self, tenant_id):
                return Evolution()

        self.post("/v1/evaluation/cases", {
            "name": None, "diff": "x", "split": None,
        }, Service())
        self.assertEqual(1, len(calls))
        self.assertEqual(("", "x", [], "validation", "api"), calls[0])
        # An explicit empty split must survive as "", not fall back to the
        # default, so that validate_case can reject it.
        self.post("/v1/evaluation/cases", {
            "name": "n", "diff": "x", "split": "",
        }, Service())
        self.assertEqual("", calls[-1][3])


class CommentDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(
            host="127.0.0.1", port=8080,
            db_path=str(Path(self.temp.name) / "test.db"),
            max_diff_bytes=10000, max_steps=8, timeout_seconds=10,
            llm_base_url="", llm_api_key="", llm_model="",
            github_webhook_secret="", github_token="", auto_post_review=False,
        )
        self.service = ReviewService(self.settings)
        self.addCleanup(self.service.queue.close)
        self.service.store.create(
            "comment-task", "org/a", 1,
            {
                "github_issue_url": "https://github.com/org/a/pull/1",
                "installation_id": 7,
            },
            "tenant-a",
        )
        self.service.store.succeed(
            "comment-task",
            ReviewReport(
                repository="org/a", pull_request=1, summary="ok", risk="low",
            ),
            TraceEvent(1, TaskState.SUCCESS, "done", utc_now()),
        )
        self.execution = self.service._reviews_app.execution
        self.execution.comments.BACKOFF_SECONDS = 0

    def test_redelivery_retries_transient_failures(self):
        calls = []

        class FlakyClient:
            def __init__(self):
                self.failures = 2

            def upsert_comment(self, url, body, marker):
                calls.append((url, marker))
                if self.failures:
                    self.failures -= 1
                    raise RuntimeError("GitHub outage")

        client = FlakyClient()
        self.execution.comments.github_client_for_installation = (
            lambda installation_id: client
        )

        result = self.service.redeliver_comment("comment-task", "tenant-a")

        self.assertTrue(result["delivered"])
        self.assertEqual(3, len(calls))
        self.assertEqual(
            review_comment_marker(
                "tenant-a", "https://github.com/org/a/pull/1", "comment-task",
            ),
            calls[-1][1],
        )

    def test_comment_marker_is_stable_per_pr_and_tenant(self):
        first = review_comment_marker(
            "tenant-a", "https://github.com/org/a/pull/1/", "task-1",
        )
        second = review_comment_marker(
            "tenant-a", "https://github.com/org/a/pull/1", "task-2",
        )
        other_tenant = review_comment_marker(
            "tenant-b", "https://github.com/org/a/pull/1", "task-1",
        )
        self.assertEqual(first, second)
        self.assertNotEqual(first, other_tenant)
        self.assertNotIn("task-1", first)

    def test_failed_redelivery_is_recorded_without_failing_the_task(self):
        class BrokenClient:
            def upsert_comment(self, url, body, marker):
                raise RuntimeError("GitHub outage")

        self.execution.comments.github_client_for_installation = (
            lambda installation_id: BrokenClient()
        )

        result = self.service.redeliver_comment("comment-task", "tenant-a")

        self.assertFalse(result["delivered"])
        self.assertEqual("SUCCESS", self.service.store.get("comment-task")["state"])
        actions = [
            item["action"] for item in self.service.store.list_audit("tenant-a")
        ]
        self.assertIn("comment.delivery_failed", actions)

    def test_audit_failure_does_not_escape_comment_delivery(self):
        class BrokenClient:
            def upsert_comment(self, url, body, marker):
                raise RuntimeError("GitHub outage")

        self.execution.comments.github_client_for_installation = (
            lambda installation_id: BrokenClient()
        )
        with patch.object(
            self.service.store, "audit", side_effect=RuntimeError("audit down"),
        ):
            result = self.service.redeliver_comment("comment-task", "tenant-a")

        self.assertFalse(result["delivered"])
        self.assertEqual("SUCCESS", self.service.store.get("comment-task")["state"])


class LegacyMigrationTests(unittest.TestCase):
    def test_existing_prompt_data_survives_tenant_migration_and_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "legacy.db")
            with sqlite3.connect(path) as conn:
                conn.execute("CREATE TABLE skill_versions(id INTEGER PRIMARY KEY AUTOINCREMENT,skill_name TEXT,"
                             "version INTEGER,prompt TEXT,score REAL,active INTEGER,parent_version INTEGER,"
                             "created_at TEXT,UNIQUE(skill_name,version))")
                conn.execute("INSERT INTO skill_versions VALUES (7,'llm-review',1,'legacy',.5,1,NULL,?)", (utc_now(),))
                conn.execute("CREATE TABLE evaluation_cases(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT UNIQUE,"
                             "split TEXT,diff TEXT,expected_json TEXT,source TEXT,active INTEGER,created_at TEXT)")
                conn.execute("INSERT INTO evaluation_cases VALUES (9,'sample','validation',?,'[]','manual',1,?)", (DIFF, utc_now()))
            conn.close()
            store = TaskStore(path)
            self.assertEqual(7, store.get_active_skill_version("llm-review")["id"])
            self.assertEqual(9, store.list_evaluation_cases()[0]["id"])
            self.assertIsNone(store.get_active_skill_version("llm-review", "tenant-b"))
            store.save_skill_version("llm-review", "other tenant", .7, True, "tenant-b")
            store.save_evaluation_case("sample", "validation", CLEAN, [], tenant_id="tenant-b")
            restarted = TaskStore(path)
            self.assertEqual("legacy", restarted.get_active_skill_version("llm-review")["prompt"])
            self.assertEqual("other tenant", restarted.get_active_skill_version("llm-review", "tenant-b")["prompt"])

    def test_legacy_rows_are_adopted_by_the_configured_default_tenant_once(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "legacy.db")
            with sqlite3.connect(path) as conn:
                conn.execute(
                    "CREATE TABLE skill_versions(id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    "skill_name TEXT,version INTEGER,prompt TEXT,score REAL,active INTEGER,"
                    "parent_version INTEGER,created_at TEXT,UNIQUE(skill_name,version))"
                )
                conn.execute(
                    "INSERT INTO skill_versions VALUES (7,'llm-review',1,'legacy',.5,1,NULL,?)",
                    (utc_now(),),
                )
                conn.execute(
                    "CREATE TABLE tasks(id TEXT PRIMARY KEY,state TEXT,repository TEXT,"
                    "pull_request INTEGER,input_json TEXT,report_json TEXT,error TEXT,"
                    "created_at TEXT,updated_at TEXT)"
                )
                conn.execute(
                    "INSERT INTO tasks VALUES ('legacy-task','SUCCESS','org/a',1,'{}',NULL,NULL,?,?)",
                    (utc_now(), utc_now()),
                )
            conn.close()
            store = TaskStore(path)
            self.assertIsNotNone(store.get_active_skill_version("llm-review"))
            self.assertIsNotNone(store.get("legacy-task"))

            self.assertTrue(store.migrate_legacy_tenant("tenant-a"))
            self.assertEqual("legacy", store.get_active_skill_version(
                "llm-review", "tenant-a")["prompt"])
            self.assertIsNone(store.get_active_skill_version("llm-review"))
            self.assertIsNotNone(store.get("legacy-task", "tenant-a"))
            self.assertIsNone(store.get("legacy-task", "default"))
            # The marker makes the migration one-shot and keeps its original
            # target even when the configured default tenant changes.
            self.assertFalse(store.migrate_legacy_tenant("tenant-b"))
            self.assertIsNotNone(store.get("legacy-task", "tenant-a"))
            self.assertIsNone(store.get("legacy-task", "tenant-b"))
            conn = sqlite3.connect(path)
            try:
                marker = conn.execute(
                    "SELECT value FROM schema_meta "
                    "WHERE key='legacy_tenant_adopted_to'"
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual("tenant-a", json.loads(marker)["target"])

    def test_conflicting_rows_are_skipped_and_adopted_after_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "legacy.db")
            store = TaskStore(path)
            store.grant_repository("default", "org/a", False)
            store.grant_repository("tenant-a", "org/a", True)

            # The target already owns the same key: startup must not crash.
            self.assertFalse(store.migrate_legacy_tenant("tenant-a"))
            conn = sqlite3.connect(path)
            try:
                default_rows = conn.execute(
                    "SELECT auto_fix FROM repository_grants "
                    "WHERE tenant_id='default' AND repository='org/a'"
                ).fetchall()
                marker = conn.execute(
                    "SELECT value FROM schema_meta WHERE key='legacy_tenant_adopted_to'"
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual([(0,)], default_rows)
            self.assertIn("repository_grants", marker)

            # Resolving the collision lets the next startup finish the job.
            store.revoke_repository("tenant-a", "org/a")
            self.assertTrue(store.migrate_legacy_tenant("tenant-a"))
            self.assertEqual(
                [{"repository": "org/a", "auto_fix": False}],
                store.list_repository_grants("tenant-a"),
            )
            conn = sqlite3.connect(path)
            try:
                remaining = conn.execute(
                    "SELECT COUNT(*) FROM repository_grants WHERE tenant_id='default'"
                ).fetchone()[0]
                marker = conn.execute(
                    "SELECT value FROM schema_meta WHERE key='legacy_tenant_adopted_to'"
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(0, remaining)
            self.assertNotIn("repository_grants", marker)

    def test_composite_unique_conflicts_do_not_abort_startup(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "legacy.db")
            store = TaskStore(path)
            store.save_skill_version("llm-review", "legacy", .5, True, tenant_id="default")
            store.save_skill_version("llm-review", "target", .7, True, tenant_id="tenant-a")

            self.assertFalse(store.migrate_legacy_tenant("tenant-a"))
            self.assertEqual(
                "legacy", store.get_active_skill_version("llm-review", "default")["prompt"],
            )
            self.assertEqual(
                "target", store.get_active_skill_version("llm-review", "tenant-a")["prompt"],
            )
            conn = sqlite3.connect(path)
            try:
                conn.execute(
                    "DELETE FROM skill_versions WHERE tenant_id='tenant-a'"
                )
                conn.commit()
            finally:
                conn.close()
            self.assertTrue(store.migrate_legacy_tenant("tenant-a"))
            self.assertEqual(
                "legacy", store.get_active_skill_version("llm-review", "tenant-a")["prompt"],
            )
            self.assertIsNone(store.get_active_skill_version("llm-review", "default"))


class _StubHandler(ApiHandler):
    """Drive ApiHandler routing without a socket."""

    def __init__(self, service, settings, path, headers=None, body=b""):
        self.service = service
        self.settings = settings
        self.path = path
        self.headers = dict(headers or {})
        self.client_address = ("127.0.0.1", 50000)
        self._body = body
        self.status = None
        self.body = None
        self.location = ""
        self.response_headers = {}

    def send_response(self, code, message=None):
        self.status = code

    def send_header(self, name, value):
        self.response_headers[name] = value
        if name.lower() == "location":
            self.location = value

    def end_headers(self):
        pass

    def _send_json(self, code, value):
        self.status = code
        self.body = value

    def _send_text(self, code, text, content_type="text/plain; charset=utf-8"):
        self.status = code
        self.body = text

    def _read_body(self):
        return self._body


class SecurityHardeningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(
            host="127.0.0.1", port=8080,
            db_path=str(Path(self.temp.name) / "security.db"),
            max_diff_bytes=10000, max_steps=8, timeout_seconds=10,
            llm_base_url="", llm_api_key="", llm_model="",
            github_webhook_secret="", github_token="", auto_post_review=False,
            auth_required=True, auth_secret="s" * 32,
            bootstrap_admin_username="admin",
            bootstrap_admin_password="Adm1n-password!",
            default_tenant_id="tenant-default",
            github_app_slug="evo-demo",
            github_setup_state="setup-pepper",
        )
        self.service = ReviewService(self.settings)
        self.addCleanup(self.service.queue.close)
        self.token = self.service.auth.login(
            "admin", "Adm1n-password!", "",
        )["access_token"]

    def call(self, path, method="GET", headers=None, body=b""):
        handler = _StubHandler(self.service, self.settings, path, headers, body)
        getattr(handler, "do_" + method)()
        return handler

    def auth_headers(self, **extra):
        values = {"Authorization": "Bearer " + self.token}
        values.update(extra)
        return values

    def test_missing_credentials_answer_401_on_get_and_post(self):
        self.assertEqual(401, self.call("/api/dashboard").status)
        self.assertEqual(401, self.call("/v1/skills/reload", "POST").status)

    def test_health_is_minimal_and_detail_requires_auth(self):
        public = self.call("/health")
        self.assertEqual(200, public.status)
        self.assertEqual({"status": "ok"}, public.body)
        self.assertEqual(401, self.call("/health/detail").status)
        detail = self.call("/health/detail", headers=self.auth_headers())
        self.assertEqual(200, detail.status)
        self.assertIn("llm_provider", detail.body)

    def test_security_headers_include_a_content_security_policy(self):
        handler = _StubHandler(self.service, self.settings, "/")
        handler._headers(200, "text/plain", 0)
        policy = handler.response_headers["Content-Security-Policy"]
        self.assertIn("default-src 'self'", policy)
        self.assertIn("object-src 'none'", policy)

    def test_github_install_requires_manage_and_state_is_one_time(self):
        anonymous = self.call("/github/install")
        self.assertEqual(401, anonymous.status)
        self.assertEqual("", anonymous.location)

        started = self.call(
            "/github/install", headers=self.auth_headers(Accept="application/json"),
        )
        self.assertEqual(200, started.status)
        state = urllib.parse.parse_qs(
            urllib.parse.urlparse(started.body["url"]).query
        )["state"][0]

        installed = self.call(
            "/github/setup?installation_id=4242&state="
            + urllib.parse.quote(state)
        )
        self.assertEqual(302, installed.status)
        self.assertEqual(
            "tenant-default", self.service.store.installation_tenant(4242),
        )

        replay = self.call(
            "/github/setup?installation_id=4243&state="
            + urllib.parse.quote(state)
        )
        self.assertEqual(403, replay.status)
        self.assertIsNone(self.service.store.installation_tenant(4243))

    def test_static_setup_state_no_longer_authorizes_anonymous_setup(self):
        response = self.call(
            "/github/setup?installation_id=5252&state="
            + self.settings.github_setup_state
        )
        self.assertEqual(403, response.status)
        self.assertIsNone(self.service.store.installation_tenant(5252))

    def test_trusted_proxy_supplies_the_client_address(self):
        settings = replace(self.settings, trusted_proxies=("127.0.0.1",))
        proxied = _StubHandler(self.service, settings, "/v1/auth/login", {
            "X-Forwarded-For": "198.51.100.7, 127.0.0.1",
        })
        self.assertEqual("198.51.100.7", proxied._client_key())
        direct = _StubHandler(self.service, settings, "/", {
            "X-Forwarded-For": "198.51.100.7",
        })
        direct.client_address = ("192.0.2.5", 5000)
        self.assertEqual("192.0.2.5", direct._client_key())

    def test_requested_mode_is_persisted_on_the_task(self):
        task_id = self.service._create_task(
            "org/a", DIFF, 1, "test", "tenant-default", mode="agentic",
        )
        task = self.service.store.get(task_id, "tenant-default")
        self.assertEqual("agentic", task["input"]["mode"])
        self.service.chat_client = object()
        with self.assertRaises(ValueError):
            self.service.create_review(
                "org/a", DIFF, tenant_id="tenant-default", mode="scanner",
            )

    def test_alert_manager_uses_the_configured_window(self):
        calls = []

        class Store:
            def dashboard_stats(self, tenant_id, window_seconds=None):
                calls.append((tenant_id, window_seconds))
                return {"tasks_total": 0, "tasks_failed": 0}

            def create_alert(self, *args):
                raise AssertionError("no alert expected")

        AlertManager(Store(), window_seconds=120).evaluate("tenant-a")
        self.assertEqual([("tenant-a", 120)], calls)

    def test_rollout_rejects_an_unreachable_shadow_band(self):
        with self.assertRaises(ValueError):
            self.service.releases.configure("tenant-default", "llm-review", {
                "candidate_version": 1,
                "canary_percent": 50,
                "shadow_percent": 10,
            })
