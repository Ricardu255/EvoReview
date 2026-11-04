"""Tests for application-service policies, feedback capture and webhook intake."""
import os
import tempfile
import unittest

from evoagent.app.feedback import FeedbackApplicationService
from evoagent.app.github import GitHubApplicationService
from evoagent.app.policies import ReviewPolicy
from evoagent.app.review_lifecycle import ReviewLifecycleService
from evoagent.config import Settings
from evoagent.errors import ModelNotConfigured


def settings(**overrides):
    values = dict(
        host="127.0.0.1", port=8080, db_path=":memory:",
        max_diff_bytes=10, max_steps=8, timeout_seconds=10,
        llm_base_url="", llm_api_key="", llm_model="",
        github_webhook_secret="", github_token="", auto_post_review=False,
    )
    values.update(overrides)
    return Settings(**values)


class _PolicyStore:
    def __init__(self, grants=True, allowed=True, auto=True):
        self.grants = grants
        self.allowed = allowed
        self.auto = auto
        self.calls = []

    def has_repository_grants(self, tenant_id):
        self.calls.append(("grants", tenant_id))
        return self.grants

    def repository_allowed(self, tenant_id, repository, require_auto_fix=False):
        self.calls.append(("allowed", tenant_id, repository, require_auto_fix))
        return self.auto if require_auto_fix else self.allowed


class _Skill:
    def __init__(self, name):
        self.name = name


class ReviewPolicyTests(unittest.TestCase):
    def policy(self, **overrides):
        store = _PolicyStore(**overrides)
        self.store = store
        return ReviewPolicy(
            settings(), store, {"tenant-a": {"org/repo": ""}},
            lambda tenant: [_Skill("skill-a")],
        )

    def test_validate_review_bounds_repository_and_diff(self):
        policy = self.policy()
        for repository, diff, message in (
            ("", "x", "repository is required"),
            ("r" * 251, "x", "at most 250"),
            ("org/repo", "", "diff is required"),
            ("org/repo", "x" * 11, "exceeds maximum"),
        ):
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    policy.validate_review(repository, diff)

    def test_require_agentic_model(self):
        ReviewPolicy.require_agentic_model(object())
        with self.assertRaises(ModelNotConfigured):
            ReviewPolicy.require_agentic_model(None)

    def test_authorize_repository_honours_grants_and_allowlists(self):
        self.policy().authorize_repository("tenant-a", "org/repo")

        restricted = ReviewPolicy(
            settings(require_repository_grants=True), _PolicyStore(grants=False),
            {}, lambda tenant: [],
        )
        with self.assertRaisesRegex(PermissionError, "grants are not configured"):
            restricted.authorize_repository("tenant-a", "org/repo")

        denied = self.policy(allowed=False)
        with self.assertRaisesRegex(PermissionError, "not authorized"):
            denied.authorize_repository("tenant-a", "org/repo")

    def test_authorize_auto_fix_requires_the_per_repository_opt_in(self):
        self.policy().authorize_auto_fix("tenant-a", "org/repo")
        policy = self.policy(auto=False)
        with self.assertRaisesRegex(PermissionError, "automatic repair"):
            policy.authorize_auto_fix("tenant-a", "org/repo")

    def test_resolve_repository_root_rules(self):
        policy = self.policy()
        self.assertEqual("", policy.resolve_repository_root("org/repo", "", "tenant-a"))
        with self.assertRaisesRegex(PermissionError, "no local checkout"):
            policy.resolve_repository_root("org/repo", "/tmp/x", "tenant-a")

        with tempfile.TemporaryDirectory() as root:
            policy.repository_roots = {"tenant-a": {"org/repo": root}}
            self.assertEqual(os.path.realpath(root),
                             policy.resolve_repository_root("org/repo", "", "tenant-a"))
            self.assertEqual(os.path.realpath(root), policy.resolve_repository_root(
                "org/repo", root, "tenant-a",
            ))
            with self.assertRaisesRegex(PermissionError, "does not match"):
                policy.resolve_repository_root("org/repo", "/elsewhere", "tenant-a")

        policy.repository_roots = {"tenant-a": {"org/repo": "/missing/dir"}}
        with self.assertRaisesRegex(ValueError, "unavailable"):
            policy.resolve_repository_root("org/repo", "", "tenant-a")

    def test_validate_enabled_agents(self):
        policy = self.policy()
        policy.validate_enabled_agents(None)
        policy.validate_enabled_agents(["lead", "critic"])
        with self.assertRaisesRegex(ValueError, "unsupported enabled Agent role"):
            policy.validate_enabled_agents(["lead", "intruder"])
        with self.assertRaisesRegex(ValueError, "requires the lead Agent"):
            policy.validate_enabled_agents(["security"])
        restricted = ReviewPolicy(
            settings(enabled_agents="lead,security"), _PolicyStore(), {},
            lambda tenant: [],
        )
        with self.assertRaisesRegex(ValueError, "disabled by EVOAGENT_ENABLED_AGENTS"):
            restricted.validate_enabled_agents(["lead", "critic"])

    def test_validate_enabled_skills(self):
        policy = self.policy()
        policy.validate_enabled_skills(None, "tenant-a")
        policy.validate_enabled_skills(["skill-a"], "tenant-a")
        with self.assertRaisesRegex(ValueError, "Skill names"):
            policy.validate_enabled_skills([1], "tenant-a")
        with self.assertRaisesRegex(ValueError, "unknown enabled Agent Skill"):
            policy.validate_enabled_skills(["other"], "tenant-a")


class _FeedbackStore:
    def __init__(self, task):
        self.task = task
        self.recorded = []

    def get(self, task_id, tenant_id=None):
        return self.task

    def record_failure_case(self, task_id, category, payload):
        self.recorded.append((task_id, category, payload))


class _Memory:
    def __init__(self):
        self.calls = []

    def remember_feedback(self, *args):
        self.calls.append(args)


class _Metrics:
    def __init__(self):
        self.counters = []

    def inc(self, name, value=1.0):
        self.counters.append((name, value))


class FeedbackServiceTests(unittest.TestCase):
    def service(self, task):
        return FeedbackApplicationService(_FeedbackStore(task), _Memory(), _Metrics())

    def test_requires_a_completed_review_task(self):
        with self.assertRaisesRegex(ValueError, "task not found"):
            self.service(None).record_feedback("t", "missed_issue", None, "")
        for task in ({"state": "RUNNING", "report": {}},
                     {"state": "SUCCESS", "report": None}):
            with self.subTest(task=task):
                with self.assertRaisesRegex(ValueError, "requires a completed"):
                    self.service(task).record_feedback("t", "missed_issue", None, "")

    def test_rejects_unknown_categories(self):
        task = {"state": "SUCCESS", "report": {"x": 1}, "repository": "org/repo"}
        with self.assertRaisesRegex(ValueError, "unsupported feedback category"):
            self.service(task).record_feedback("t", "unknown", None, "")

    def test_records_persisted_feedback_memory_and_metrics(self):
        task = {
            "state": "SUCCESS", "report": {"x": 1},
            "repository": "org/repo", "tenant_id": "tenant-a",
        }
        service = self.service(task)
        result = service.record_feedback(
            "t", "missed_issue", {"rule_id": "X"}, "n" * 3000,
        )
        self.assertEqual({"recorded": True, "category": "missed_issue"}, result)
        task_id, category, payload = service.store.recorded[0]
        self.assertEqual(("t", "missed_issue"), (task_id, category))
        self.assertEqual(2000, len(payload["note"]))
        self.assertEqual(2000, len(service.memory.calls[0][-1]))
        self.assertEqual([("feedback_total", 1.0)], service.metrics.counters)

    def test_tenant_falls_back_to_the_task_tenant(self):
        task = {
            "state": "SUCCESS", "report": {"x": 1},
            "repository": "org/repo", "tenant_id": "task-tenant",
        }
        service = self.service(task)
        service.record_feedback("t", "accepted", None, "", tenant_id="caller-tenant")
        self.assertEqual("task-tenant", service.memory.calls[0][0])


class _WebhookStore:
    def __init__(self):
        self.bindings = {}
        self.claims = True
        self.webhooks = {}
        self.tasks = {}
        self.events = []

    def installation_tenant(self, installation_id):
        return self.bindings.get(installation_id)

    def claim_webhook(self, *args):
        self.events.append(("claim",) + args)
        return self.claims

    def get_webhook(self, delivery_id):
        return self.webhooks.get(delivery_id)

    def get(self, task_id, tenant_id=None):
        return self.tasks.get(task_id)

    def bind_webhook_task(self, *args):
        self.events.append(("bind",) + args)

    def complete_webhook(self, *args):
        self.events.append(("complete",) + args)

    def release_webhook(self, *args):
        self.events.append(("release",) + args)


class _Queue:
    backend = "inline"

    def __init__(self, fail=False):
        self.submits = []
        self.fail = fail

    def submit(self, payload, message_id=None):
        if self.fail:
            raise RuntimeError("queue down")
        self.submits.append((payload, message_id))


class _WebhookPolicy:
    def __init__(self):
        self.calls = []

    def require_agentic_model(self, client):
        self.calls.append(("model", client))

    def authorize_repository(self, tenant_id, repository):
        self.calls.append(("authorize", tenant_id, repository))


class _SmallMetrics:
    def __init__(self):
        self.counters = []

    def inc(self, name, value=1.0):
        self.counters.append((name, value))


class _BrokenQueue:
    backend = "memory"

    def submit(self, payload, message_id=None):
        raise RuntimeError("queue unavailable")


class _LifecycleStore:
    def __init__(self):
        self.tasks = {}

    def create(self, task_id, repository, pull_request, payload, tenant_id="default"):
        self.tasks[task_id] = {
            "id": task_id, "repository": repository, "pull_request": pull_request,
            "input": dict(payload), "tenant_id": tenant_id,
            "state": "PENDING", "trace": [], "error": "",
        }

    def save_task_payload(self, task_id, diff):
        self.tasks[task_id]["diff"] = diff

    def update_task_input(self, task_id, updates):
        self.tasks[task_id]["input"].update(updates)

    def get(self, task_id, tenant_id=None):
        return self.tasks.get(task_id)

    def fail(self, task_id, error, event):
        task = self.tasks[task_id]
        task["state"] = "FAILED"
        task["error"] = error
        task["trace"].append(event)


class _LifecycleExecution:
    def _execution_assignment(self, tenant_id, task_id):
        return {
            "release_lane": "stable", "shadow": False,
            "prompt_version": 0, "shadow_version": None,
        }


class ReviewLifecycleFailureTests(unittest.TestCase):
    def test_queue_submit_failure_marks_the_created_task_failed(self):
        store = _LifecycleStore()
        service = ReviewLifecycleService(
            store, _BrokenQueue(),
            ReviewPolicy(settings(), _PolicyStore(), {}, lambda tenant: []),
            object(), object(), object(), _SmallMetrics(), lambda: object(),
            _LifecycleExecution(),
        )
        with self.assertRaisesRegex(RuntimeError, "queue unavailable"):
            service.enqueue_review("org/repo", "diff", 1, tenant_id="tenant-a")
        task = next(iter(store.tasks.values()))
        self.assertEqual("FAILED", task["state"])
        self.assertIn("queue unavailable", task["error"])


class GitHubApplicationServiceTests(unittest.TestCase):
    def service(self, **overrides):
        self.store = _WebhookStore()
        self.queue = _Queue(fail=overrides.pop("queue_fail", False))
        self.policy = _WebhookPolicy()
        self.metrics = _SmallMetrics()
        self.deferred = []
        self.settings = settings(**overrides)

        def create_deferred(repository, number, source, tenant_id, payload, task_id=None):
            self.deferred.append((repository, number, source, tenant_id, payload, task_id))

        self.service_instance = GitHubApplicationService(
            self.settings, self.store, self.queue, self.policy,
            lambda: "model", "default-client",
            lambda installation_id: "installation-%d" % installation_id,
            create_deferred, self.metrics,
        )
        return self.service_instance

    def payload(self, **overrides):
        values = {
            "installation": {"id": 7},
            "action": "opened",
            "number": 12,
            "repository": {"full_name": "org/repo"},
            "pull_request": {
                "diff_url": "https://github.test/diff",
                "issue_url": "https://github.test/issue",
                "head": {"sha": "abc123"},
            },
        }
        values.update(overrides)
        return values

    def test_invalid_installation_ids_are_rejected(self):
        service = self.service()
        with self.assertRaisesRegex(ValueError, "invalid GitHub installation id"):
            service.handle_github_pull_request(
                self.payload(installation={"id": "abc"}), "d1", "sha",
            )

    def test_unbound_installations_are_rejected_by_default(self):
        service = self.service()
        with self.assertRaisesRegex(PermissionError, "not bound"):
            service.handle_github_pull_request(self.payload(), "d1", "sha")

    def test_bound_installations_cannot_cross_tenants(self):
        service = self.service()
        self.store.bindings[7] = "tenant-a"
        with self.assertRaisesRegex(PermissionError, "different tenant"):
            service.handle_github_pull_request(
                self.payload(), "d1", "sha", tenant_id="tenant-b",
            )

    def test_unsupported_actions_are_ignored_and_claimed(self):
        service = self.service(allow_unbound_installations=True)
        result = service.handle_github_pull_request(
            self.payload(action="closed"), "d1", "sha",
        )
        self.assertTrue(result["ignored"])
        self.assertIn("closed", result["reason"])
        self.assertEqual([], self.policy.calls)
        self.assertTrue(any(event[0] == "complete" for event in self.store.events))

    def test_duplicate_deliveries_report_the_existing_task(self):
        service = self.service(allow_unbound_installations=True)
        self.store.claims = False
        self.store.webhooks["d1"] = {"task_id": "task-1"}
        result = service.handle_github_pull_request(self.payload(), "d1", "sha")
        self.assertEqual({"duplicate": True, "task_id": "task-1", "state": "PENDING"}, result)

    def test_duplicate_without_a_task_is_accepted(self):
        service = self.service(allow_unbound_installations=True)
        self.store.claims = False
        result = service.handle_github_pull_request(self.payload(), "d1", "sha")
        self.assertEqual("ACCEPTED", result["state"])
        self.assertIsNone(result["task_id"])

    def test_invalid_supported_payloads_are_rejected(self):
        service = self.service(allow_unbound_installations=True)
        for payload in (
            self.payload(repository={}),
            self.payload(number="12"),
            self.payload(pull_request={"issue_url": "x"}),
        ):
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(ValueError, "invalid GitHub pull_request"):
                    service.handle_github_pull_request(payload, "d1", "sha")

    def test_supported_deliveries_create_and_enqueue_one_task(self):
        service = self.service(
            allow_unbound_installations=True, auto_post_review=True,
        )
        result = service.handle_github_pull_request(self.payload(), "d1", "sha")
        self.assertEqual("PENDING", result["state"])
        self.assertEqual("inline", result["queue"])
        self.assertTrue(result["will_post_to_github"])
        self.assertTrue(result["task_id"])
        self.assertEqual(1, len(self.deferred))
        self.assertEqual(["model"], [call[1] for call in self.policy.calls if call[0] == "model"])
        self.assertEqual("org/repo", self.deferred[0][0])
        self.assertEqual(result["task_id"], self.queue.submits[0][1])
        self.assertEqual("abc123", self.queue.submits[0][0]["head_sha"])
        self.assertEqual([("reviews_enqueued_total", 1.0)], self.metrics.counters)
        self.assertIn("bind", [event[0] for event in self.store.events])

    def test_existing_tasks_are_reused(self):
        service = self.service(allow_unbound_installations=True)
        # Compute the deterministic id the service will use.
        import uuid
        task_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "evoagent:webhook:default:d1"))
        self.store.tasks[task_id] = {"id": task_id}
        service.handle_github_pull_request(self.payload(), "d1", "sha")
        self.assertEqual([], self.deferred)

    def test_queue_failures_release_the_webhook_claim(self):
        service = self.service(allow_unbound_installations=True, queue_fail=True)
        with self.assertRaisesRegex(RuntimeError, "queue down"):
            service.handle_github_pull_request(self.payload(), "d2", "sha")
        self.assertIn("release", [event[0] for event in self.store.events])

    def test_client_selection_uses_the_installation(self):
        service = self.service()
        self.assertEqual("default-client", service.github_client_for_installation())
        self.assertEqual("installation-9", service.github_client_for_installation(9))


if __name__ == "__main__":
    unittest.main()
