"""Behavioral regressions for isolation, resumability and actual release execution."""
from dataclasses import replace
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
import uuid
from unittest.mock import patch

from agentic_fake import FakeAgenticClient, enable_agentic_service
from evoagent.api import ApiHandler
from evoagent.adapters.auth import Principal
from evoagent.config import Settings
from evoagent.domain.diff_parser import parse_unified_diff
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
        self.assertIn("<!-- evoagent-review:%s -->" % task_id, github.comments[0][2])

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
