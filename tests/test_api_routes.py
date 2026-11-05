"""Route-level tests for evoagent.api using a socket-free handler.

These cover the read/write endpoints that the service-level tests do not
exercise: dashboards, audit/failure views, grants, dead letters, evaluation and
evolution routes, task feedback/report, webhook validation and input errors.
"""
import hashlib
import hmac
import io
import json
import signal
import time
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import patch

from evoagent.adapters.auth import Principal
from evoagent.api import (
    ApiHandler, body_optional, install_shutdown_handlers, text_field,
)
from evoagent.config import Settings


class FakeStore:
    def __init__(self):
        self.calls = []
        self.audit_events = []
        self.task = None
        self.healthy = True

    def health_check(self):
        return self.healthy

    def _record(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        return []

    def dashboard_stats(self, tenant_id):
        self._record("dashboard_stats", tenant_id)
        return {"tasks_total": 1, "tasks_failed": 0}

    def list_tasks(self, limit, tenant_id, state="", repository=""):
        self._record("list_tasks", limit, tenant_id, state, repository)
        return [{"id": "task-1"}]

    def list_failure_cases(self, unresolved_only, limit, tenant_id=None):
        self._record("list_failure_cases", unresolved_only, limit, tenant_id)
        return [{"id": 1}]

    def list_audit(self, tenant_id, limit):
        self._record("list_audit", tenant_id, limit)
        return [{"event": "review.create"}]

    def list_alerts(self, tenant_id):
        self._record("list_alerts", tenant_id)
        return [{"kind": "review-failure-rate"}]

    def list_users(self, tenant_id):
        self._record("list_users", tenant_id)
        return [{"username": "alice", "role": "maintainer", "active": True}]

    def resolve_alert(self, tenant_id, alert_id):
        self._record("resolve_alert", tenant_id, alert_id)
        return alert_id == 1

    def create_tenant(self, tenant_id):
        self._record("create_tenant", tenant_id)
        return tenant_id == "new-tenant"

    def list_tenants(self):
        self._record("list_tenants")
        return [{"tenant_id": "default"}, {"tenant_id": "tenant-a"}]

    def tenant_exists(self, tenant_id):
        self._record("tenant_exists", tenant_id)
        return tenant_id in {"default", "tenant-a", "new-tenant"}

    def set_platform_admin(self, username, active):
        self._record("set_platform_admin", username, active)
        return username == "alice"

    def list_repository_grants(self, tenant_id):
        self._record("list_repository_grants", tenant_id)
        return [{"repository": "org/repo", "auto_fix": False}]

    def get_deployment(self, tenant_id, skill_name):
        self._record("get_deployment", tenant_id, skill_name)
        return {"stable_version": 1}

    def list_evaluation_cases(self, split, include, limit, tenant_id=None):
        self._record("list_evaluation_cases", split, include, limit, tenant_id)
        return [{"id": "case-1"}]

    def list_evolution_runs(self, limit, tenant_id):
        self._record("list_evolution_runs", limit, tenant_id)
        return [{"id": "run-1"}]

    def list_skill_evolution_runs(self, limit, tenant_id):
        self._record("list_skill_evolution_runs", limit, tenant_id)
        return [{"id": "run-2"}]

    def list_skill_artifact_versions(self, name, tenant_id):
        self._record("list_skill_artifact_versions", name, tenant_id)
        return [{"version": 1}]

    def list_skill_versions(self, name, tenant_id):
        self._record("list_skill_versions", name, tenant_id)
        return [{
            "version": 1, "score": 1.0, "active": True,
            "parent_version": None, "created_at": "2026-01-01",
            "prompt": "prompt text",
        }]

    def get(self, task_id, tenant_id):
        self._record("get", task_id, tenant_id)
        if self.task and task_id == self.task.get("id"):
            return self.task
        return None

    def list_task_failure_cases(self, task_id, tenant_id):
        self._record("list_task_failure_cases", task_id, tenant_id)
        return [{"id": 3}]

    def grant_repository(self, tenant_id, repository, auto_fix):
        self._record("grant_repository", tenant_id, repository, auto_fix)

    def revoke_repository(self, tenant_id, repository):
        self._record("revoke_repository", tenant_id, repository)
        return repository == "org/repo"

    def audit(self, *args):
        self.audit_events.append(args)

    def save_github_setup_state(self, *args):
        pass

    def consume_github_setup_state(self, token):
        return ""

    def installation_tenant(self, installation_id):
        return None

    def save_installation(self, *args):
        pass


class FakeQueue:
    backend = "inline"

    def __init__(self):
        self.replayed = []
        self.healthy = True

    def health_check(self):
        return self.healthy

    def dead_letters(self, limit, tenant_id):
        return {"limit": limit, "tenant": tenant_id, "messages": []}

    def replay_dead_letter(self, message_id, tenant_id):
        self.replayed.append((message_id, tenant_id))
        return message_id == "ok"


class FakeEvolution:
    def __init__(self):
        self.decision = "deferred"
        self.calls = []

    def status(self):
        return {"ready": True}

    def add_evaluation_case(self, name, diff, expected, split, source):
        self.calls.append(("add", name, diff, expected, split, source))
        return {"name": name, "split": split}

    def auto_propose(self, skill_name, tenant_id=None):
        self.calls.append(("auto", skill_name, tenant_id))
        return {"decision": self.decision, "run_id": "run-1"}

    def propose(self, skill_name, prompt, score=None):
        self.calls.append(("propose", skill_name, prompt, score))
        return {"decision": self.decision, "run_id": "run-2"}

    def rollback(self, skill_name, version):
        self.calls.append(("rollback", skill_name, version))
        return version == 1


class FakeSkillEvolution:
    def __init__(self):
        self.decision = "deferred"
        self.calls = []

    def status(self, skill_name, tenant_id):
        return {"skill_name": skill_name, "ready": True}

    def auto_propose(self, skill_name, tenant_id=None):
        self.calls.append(("auto", skill_name, tenant_id))
        return {"decision": self.decision, "run_id": "srun-1"}

    def propose(self, skill_name, artifact, tenant_id=None):
        self.calls.append(("propose", skill_name, artifact, tenant_id))
        return {"decision": self.decision, "run_id": "srun-2"}

    def rollback(self, skill_name, version, tenant_id=None):
        self.calls.append(("rollback", skill_name, version, tenant_id))
        return version == 1


class FakeReleases:
    def __init__(self):
        self.calls = []

    def configure(self, tenant_id, skill_name, payload):
        self.calls.append((tenant_id, skill_name, payload))
        return {"configured": payload}


class FakeAuth:
    def __init__(self):
        self.calls = []

    def authenticate(self, authorization):
        return Principal("u", "admin", "default", "admin")

    @staticmethod
    def require(principal, permissions):
        if any(not principal.can(item) for item in permissions):
            raise PermissionError("permission denied")

    def list_tenant_users(self, tenant_id):
        self.calls.append(("list", tenant_id))
        return [{"username": "alice", "role": "maintainer", "active": True}]

    def issue_token(self, username, tenant_id, ttl_seconds=None):
        self.calls.append(("token", username, tenant_id, ttl_seconds))
        return {
            "access_token": "issued-token", "token_type": "Bearer",
            "expires_in": ttl_seconds or 100, "tenant_id": tenant_id,
            "role": "maintainer",
        }

    def create_tenant_user(self, username, password, tenant_id, role):
        self.calls.append(("create", username, password, tenant_id, role))
        return {
            "username": username, "tenant_id": tenant_id, "role": role,
            "created": True,
        }

    def set_tenant_user_password(self, username, password, tenant_id):
        self.calls.append(("password", username, password, tenant_id))
        return username == "alice"

    def set_tenant_user_active(self, username, tenant_id, active):
        self.calls.append(("active", username, tenant_id, active))
        return username == "alice"


class FakeQuota:
    def __init__(self):
        self.calls = []

    def status(self, tenant_id):
        return {
            "tenant_id": tenant_id, "day": "2026-01-01",
            "limits": {"daily_reviews": 10, "daily_tokens": 1000,
                       "daily_cost_usd": 1.0},
            "usage": {"reviews": 1, "tokens": 10, "cost_usd": 0.1},
            "remaining": {"daily_reviews": 9, "daily_tokens": 990,
                          "daily_cost_usd": 0.9},
        }

    def set_limits(self, tenant_id, reviews, tokens, cost):
        self.calls.append((tenant_id, reviews, tokens, cost))
        return self.status(tenant_id)


class FakeService:
    def __init__(self):
        self.store = FakeStore()
        self.queue = FakeQueue()
        self.auth = FakeAuth()
        self.quota = FakeQuota()
        self.evolution = FakeEvolution()
        self.skill_evolution = FakeSkillEvolution()
        self.releases = FakeReleases()
        self.llm_config = {"provider": "local", "model": ""}
        self.reviewer = _Named("local-rules")
        self.harness = _Named("runtime")
        self.review_calls = []
        self.enqueue_calls = []
        self.fix_calls = []
        self.feedback_calls = []
        self.cancel_result = True
        self.comment_result = {"delivered": True}
        self.reload_count = 0
        self.webhook_calls = []

    def list_skills(self, tenant_id):
        return [{"name": "skill"}]

    def create_review(self, repository, diff, pull_request, **options):
        self.review_calls.append((repository, diff, pull_request, options))
        return {"id": "new-task"}

    def enqueue_review(self, repository, diff, pull_request, **options):
        self.enqueue_calls.append((repository, diff, pull_request, options))
        return {"task_id": "queued-task"}

    def reload_skills(self):
        self.reload_count += 1
        return [{"name": "reloaded"}]

    def handle_github_pull_request(self, payload, delivery_id, digest):
        self.webhook_calls.append((payload, delivery_id, digest))
        return {"accepted": True}

    def create_fix(self, task_id, installation_id, tenant_id):
        self.fix_calls.append((task_id, installation_id, tenant_id))
        return {"branch": "evoagent/fix"}

    def record_feedback(self, task_id, category, finding, note, tenant_id):
        self.feedback_calls.append((task_id, category, finding, note, tenant_id))
        return {"category": category}

    def cancel_task(self, task_id, tenant_id):
        return self.cancel_result

    def resume_task(self, task_id, tenant_id):
        return {"resumed": True}

    def redeliver_comment(self, task_id, tenant_id):
        return self.comment_result

    def evolution_for(self, tenant_id):
        return self.evolution


class _Named:
    def __init__(self, name):
        self.name = name


class Handler(ApiHandler):
    def __init__(self, service, settings, path, headers=None, body=b""):
        self.service = service
        self.settings = settings
        self.path = path
        self.headers = dict(headers or {})
        self.client_address = ("127.0.0.1", 50000)
        self._body = body
        self.status = None
        self.body = None
        self.text = None
        self.content_type = ""
        self.location = ""
        self.response_headers = {}
        self.wfile = io.BytesIO()

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
        self.text = text
        self.body = text
        self.content_type = content_type

    def _read_body(self):
        return self._body


def settings(**overrides):
    values = dict(
        host="127.0.0.1", port=8080, db_path=":memory:",
        max_diff_bytes=10000, max_steps=8, timeout_seconds=10,
        llm_base_url="", llm_api_key="", llm_model="",
        github_webhook_secret="", github_token="", auto_post_review=False,
        report_max_findings=50,
    )
    values.update(overrides)
    return Settings(**values)


class ApiRouteTestCase(unittest.TestCase):
    def setUp(self):
        self.service = FakeService()
        self.settings = settings()
        self.addCleanup(lambda: None)

    def call(self, path, method="GET", payload=None, headers=None, body=None):
        if body is None and payload is not None:
            body = json.dumps(payload).encode("utf-8")
        handler = Handler(self.service, self.settings, path, headers, body or b"")
        getattr(handler, "do_" + method)()
        return handler


class GetRouteTests(ApiRouteTestCase):
    def test_public_health_and_assets(self):
        self.assertEqual(200, self.call("/health").status)
        self.assertEqual({"status": "ok"}, self.call("/health").body)
        for path in ("/", "/assets/app.css", "/assets/login.css", "/assets/app.js"):
            with self.subTest(path=path):
                response = self.call(path)
                self.assertEqual(200, response.status)
                self.assertTrue(response.wfile.getvalue())

    def test_missing_or_escaping_static_files_return_404(self):
        handler = Handler(self.service, self.settings, "/")
        handler._serve_file("missing-file.html")
        self.assertEqual(404, handler.status)
        handler._serve_file("../api.py")
        self.assertEqual(404, handler.status)

    def test_health_detail_and_metrics(self):
        detail = self.call("/health/detail")
        self.assertEqual(200, detail.status)
        self.assertEqual("local-rules", detail.body["reviewer"])
        self.assertEqual("runtime", detail.body["runtime"])
        self.assertEqual("inline", detail.body["queue"])

        metrics = self.call("/metrics")
        self.assertEqual(200, metrics.status)
        self.assertIn("text/plain; version=0.0.4", metrics.content_type)
        self.assertIn("\n", metrics.text)

        self.service.store.healthy = False
        degraded = self.call("/health/detail")
        self.assertEqual(503, degraded.status)
        self.assertEqual("degraded", degraded.body["status"])
        self.assertFalse(degraded.body["dependencies"]["store"])
        self.service.store.healthy = True

    def test_sigterm_shutdown_delegates_to_the_server(self):
        class Server:
            def __init__(self):
                self.called = False

            def shutdown(self):
                self.called = True

        server = Server()
        with patch("evoagent.api.signal.signal") as register:
            install_shutdown_handlers(server)
        handler = register.call_args[0][1]
        handler(signal.SIGTERM, None)
        deadline = time.monotonic() + 1
        while not server.called and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(server.called)

    def test_dashboard_and_list_routes(self):
        dashboard = self.call("/api/dashboard")
        self.assertEqual(200, dashboard.status)
        self.assertEqual(1, dashboard.body["stats"]["tasks_total"])
        self.assertEqual("inline", dashboard.body["queue"])

        tasks = self.call("/api/tasks?limit=5")
        self.assertEqual(200, tasks.status)
        self.assertEqual([{"id": "task-1"}], tasks.body["tasks"])
        self.assertIn(
            ("list_tasks", (5, "default", "", ""), {}),
            self.service.store.calls,
        )
        filtered = self.call("/api/tasks?state=failed&repository=org/repo")
        self.assertEqual(200, filtered.status)
        self.assertIn(
            ("list_tasks", (50, "default", "FAILED", "org/repo"), {}),
            self.service.store.calls,
        )
        self.assertEqual(400, self.call("/api/tasks?state=unknown").status)

        self.assertEqual(200, self.call("/api/skills").status)
        self.assertEqual(200, self.call("/api/failures").status)
        self.assertEqual(200, self.call("/api/audit?limit=3").status)
        self.assertEqual(200, self.call("/api/alerts").status)
        self.assertEqual(200, self.call("/v1/users").status)
        self.assertEqual(200, self.call("/v1/tenant-quota").status)
        self.assertEqual(200, self.call("/v1/tenants").status)
        self.assertEqual(200, self.call("/v1/repository-grants").status)
        self.assertEqual(200, self.call("/api/deployments/llm-review").status)
        self.assertEqual(200, self.call("/api/queue/dead-letters?limit=2").status)
        self.assertEqual(200, self.call("/v1/evolution/runs?limit=4").status)
        self.assertEqual(200, self.call("/v1/evolution/status").status)
        versions = self.call("/v1/skills/llm-review/versions")
        self.assertEqual(200, versions.status)
        self.assertEqual(1, versions.body["versions"][0]["version"])
        self.assertNotIn("prompt", versions.body["versions"][0])
        self.assertIn("prompt_preview", versions.body["versions"][0])
        self.assertEqual(200, self.call("/v1/skill-evolution/status?skill_name=x").status)
        self.assertEqual(200, self.call("/v1/skill-evolution/runs").status)
        self.assertEqual(200, self.call("/v1/skill-evolution/skill/versions").status)

    def test_invalid_query_limits_are_rejected(self):
        for path in (
            "/api/tasks?limit=0",
            "/api/tasks?limit=nope",
            "/api/audit?limit=-1",
            "/api/queue/dead-letters?limit=x",
        ):
            with self.subTest(path=path):
                self.assertEqual(400, self.call(path).status)

    def test_holdout_cases_are_never_exposed(self):
        self.assertEqual(403, self.call("/v1/evaluation/cases?split=holdout").status)
        response = self.call("/v1/evaluation/cases?split=validation")
        self.assertEqual(200, response.status)
        self.assertEqual([{"id": "case-1"}], response.body["cases"])

    def test_task_feedback_report_and_not_found(self):
        self.assertEqual(404, self.call("/v1/tasks/abcdef01").status)
        self.assertEqual(404, self.call("/v1/tasks/abcdef01/report").status)
        self.assertEqual(404, self.call("/v1/tasks/abcdef01/sarif").status)
        self.assertEqual(404, self.call("/v1/tasks/abcdef01/feedback").status)

        self.service.store.task = {
            "id": "abcdef01", "report": {
                "repository": "org/repo", "pull_request": 1,
                "summary": "clean", "risk": "low", "findings": [],
            },
        }
        task = self.call("/v1/tasks/abcdef01")
        self.assertEqual(200, task.status)
        self.assertEqual("abcdef01", task.body["id"])

        report = self.call("/v1/tasks/abcdef01/report")
        self.assertEqual(200, report.status)
        self.assertIn("# EvoReview PR Review", report.text)
        self.assertIn("text/markdown", report.content_type)

        sarif = self.call("/v1/tasks/abcdef01/sarif")
        self.assertEqual(200, sarif.status)
        self.assertEqual("2.1.0", sarif.body["version"])
        self.assertEqual([], sarif.body["runs"][0]["results"])

        feedback = self.call("/v1/tasks/abcdef01/feedback")
        self.assertEqual(200, feedback.status)
        self.assertEqual([{"id": 3}], feedback.body["cases"])

    def test_unknown_route_returns_404(self):
        self.assertEqual(404, self.call("/v1/unknown").status)

    def test_github_setup_routes_are_disabled_without_configuration(self):
        self.assertEqual(503, self.call("/github/install").status)
        self.assertEqual(503, self.call("/github/setup?installation_id=1").status)

    def test_viewer_permissions_block_management_routes(self):
        def auditor(self, permission="read"):
            return Principal("u", "u", "default", "auditor")

        for path in (
            "/v1/repository-grants", "/api/queue/dead-letters",
            "/v1/skills/llm-review/versions",
            "/v1/skill-evolution/status", "/v1/skill-evolution/runs",
            "/v1/skill-evolution/x/versions",
        ):
            with self.subTest(path=path), self._as(auditor):
                self.assertEqual(403, self.call(path).status)

    # -- helper -----------------------------------------------------------
    class _as:
        def __init__(self, func):
            self.func = func

        def __enter__(self):
            from unittest.mock import patch
            self.patch = patch.object(ApiHandler, "_principal", self.func)
            self.patch.start()

        def __exit__(self, *exc):
            self.patch.stop()


class PostRouteTests(ApiRouteTestCase):
    def test_login_is_disabled_when_auth_is_off(self):
        response = self.call("/v1/auth/login", "POST", {
            "username": "u", "password": "p",
        })
        self.assertEqual(409, response.status)

    def test_review_creation_passes_options_and_audits(self):
        response = self.call("/v1/reviews", "POST", {
            "repository": "org/repo", "diff": "x", "pull_request": 3,
            "mode": "agentic", "repository_root": "/srv/repo",
            "enabled_agents": ["security"], "enabled_skills": ["s1"],
        })
        self.assertEqual(201, response.status)
        self.assertEqual({"id": "new-task"}, response.body)
        repository, diff, pr, options = self.service.review_calls[0]
        self.assertEqual(("org/repo", "x", 3), (repository, diff, pr))
        self.assertEqual("agentic", options["mode"])
        self.assertEqual(["security"], options["enabled_agents"])
        self.assertEqual(["s1"], options["enabled_skills"])
        self.assertEqual("default", options["tenant_id"])
        self.assertTrue(self.service.store.audit_events)

        queued = self.call("/v1/reviews?async=true", "POST", {
            "repository": "org/repo", "diff": "x",
        })
        self.assertEqual(202, queued.status)
        self.assertEqual(1, len(self.service.enqueue_calls))

    def test_review_audit_failure_does_not_double_send(self):
        def broken_audit(*args):
            raise RuntimeError("audit unavailable")

        self.service.store.audit = broken_audit
        response = self.call("/v1/reviews", "POST", {
            "repository": "org/repo", "diff": "x",
        })
        self.assertEqual(500, response.status)
        self.assertEqual(1, len(self.service.review_calls))

    def test_review_payload_validation(self):
        for payload in (
            {"repository": "org/repo", "diff": "x", "pull_request": "3"},
            {"repository": "org/repo", "diff": "x", "enabled_agents": "security"},
            {"repository": "org/repo", "diff": "x", "enabled_agents": [1]},
            {"repository": "org/repo", "diff": "x", "enabled_skills": "s"},
            {"repository": "org/repo", "diff": "x", "enabled_skills": [1]},
        ):
            with self.subTest(payload=payload):
                self.assertEqual(400, self.call("/v1/reviews", "POST", payload).status)

    def test_webhook_event_secret_and_signature_checks(self):
        unsupported = self.call(
            "/webhooks/github", "POST", {"x": 1},
            headers={"X-GitHub-Event": "push"},
        )
        self.assertEqual(202, unsupported.status)
        self.assertTrue(unsupported.body["ignored"])

        disabled = self.call(
            "/webhooks/github", "POST", {"x": 1},
            headers={"X-GitHub-Event": "pull_request"},
        )
        self.assertEqual(503, disabled.status)

        self.settings = settings(github_webhook_secret="secret")
        invalid = self.call(
            "/webhooks/github", "POST", {"x": 1},
            headers={"X-GitHub-Event": "pull_request"},
        )
        self.assertEqual(401, invalid.status)

    def test_webhook_accepts_recent_signed_deliveries(self):
        self.settings = settings(github_webhook_secret="secret")
        payload = {"pull_request": {
            "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }}
        body = json.dumps(payload).encode("utf-8")
        signature = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
        response = self.call(
            "/webhooks/github", "POST", headers={
                "X-GitHub-Event": "pull_request",
                "X-Hub-Signature-256": signature,
                "X-GitHub-Delivery": "delivery-1",
            }, body=body,
        )
        self.assertEqual(202, response.status)
        self.assertEqual({"accepted": True}, response.body)
        self.assertEqual(hashlib.sha256(body).hexdigest(), self.service.webhook_calls[0][2])

    def test_webhook_rejects_stale_replays_and_invalid_timestamps(self):
        self.settings = settings(github_webhook_secret="secret")
        for updated_at, expected in (
            ("2000-01-01T00:00:00Z", 409),
            ("not-a-date", 400),
        ):
            with self.subTest(updated_at=updated_at):
                body = json.dumps({"pull_request": {"updated_at": updated_at}}).encode()
                signature = "sha256=" + hmac.new(
                    b"secret", body, hashlib.sha256,
                ).hexdigest()
                response = self.call(
                    "/webhooks/github", "POST", headers={
                        "X-GitHub-Event": "pull_request",
                        "X-Hub-Signature-256": signature,
                    }, body=body,
                )
                self.assertEqual(expected, response.status)

    def test_fix_feedback_cancel_resume_and_comment_routes(self):
        self.assertEqual(400, self.call(
            "/v1/tasks/abcdef01/fix", "POST", {"installation_id": "x"},
        ).status)
        fixed = self.call(
            "/v1/tasks/abcdef01/fix", "POST", {"installation_id": 7},
        )
        self.assertEqual(201, fixed.status)
        self.assertEqual(("abcdef01", 7, "default"), self.service.fix_calls[0])

        feedback = self.call(
            "/v1/tasks/abcdef01/feedback", "POST",
            {"category": "missed_issue", "finding": {"a": 1}, "note": "n"},
        )
        self.assertEqual(201, feedback.status)
        self.assertEqual("missed_issue", feedback.body["category"])

        self.assertEqual(202, self.call("/v1/tasks/abcdef01/cancel", "POST").status)
        self.service.cancel_result = False
        self.assertEqual(404, self.call("/v1/tasks/abcdef01/cancel", "POST").status)
        self.assertEqual(202, self.call("/v1/tasks/abcdef01/resume", "POST").status)

        self.assertEqual(200, self.call("/v1/tasks/abcdef01/comment", "POST").status)
        self.service.comment_result = {"delivered": False}
        self.assertEqual(502, self.call("/v1/tasks/abcdef01/comment", "POST").status)

    def test_repository_grant_validation_and_creation(self):
        for payload in (
            {"repository": ""},
            {"repository": "x" * 251},
            {"repository": "org/repo", "auto_fix": "yes"},
        ):
            with self.subTest(payload=payload):
                self.assertEqual(400, self.call(
                    "/v1/repository-grants", "POST", payload,
                ).status)
        response = self.call(
            "/v1/repository-grants", "POST",
            {"repository": "org/repo", "auto_fix": True},
        )
        self.assertEqual(201, response.status)
        self.assertEqual({"repository": "org/repo", "auto_fix": True}, response.body)

    def test_operations_routes(self):
        reload = self.call("/v1/skills/reload", "POST")
        self.assertEqual(200, reload.status)
        self.assertEqual(1, self.service.reload_count)

        deployment = self.call(
            "/v1/deployments/llm-review", "POST", {"canary_percent": 10},
        )
        self.assertEqual(201, deployment.status)
        self.assertEqual(10, self.service.releases.calls[0][2]["canary_percent"])

        replay = self.call(
            "/v1/queue/dead-letters/replay", "POST", {"message_id": "ok"},
        )
        self.assertEqual(202, replay.status)
        missing = self.call(
            "/v1/queue/dead-letters/replay", "POST", {"message_id": "missing"},
        )
        self.assertEqual(404, missing.status)

    def test_evaluation_case_route_defaults_to_validation(self):
        response = self.call("/v1/evaluation/cases", "POST", {
            "name": "case", "diff": "--- a/a.py\n+++ b/a.py\n@@ -0,0 +1 @@\n+x\n",
            "expected_findings": [],
        })
        self.assertEqual(201, response.status)
        self.assertEqual("case", response.body["name"])
        self.assertEqual("validation", self.service.evolution.calls[0][4])
        self.assertEqual("api", self.service.evolution.calls[0][5])

    def test_evolution_routes_reload_skills_after_activation(self):
        self.service.evolution.decision = "activated"
        auto = self.call("/v1/evolution/auto", "POST", {})
        self.assertEqual(201, auto.status)
        self.assertEqual(1, self.service.reload_count)

        proposed = self.call("/v1/evolution/propose", "POST", {
            "skill_name": "llm-review", "prompt": "p", "regression_score": 0.2,
        })
        self.assertEqual(201, proposed.status)
        self.assertEqual(2, self.service.reload_count)
        self.assertEqual(0.2, self.service.evolution.calls[-1][3])

        self.service.evolution.decision = "deferred"
        deferred = self.call("/v1/evolution/propose", "POST", {
            "skill_name": "llm-review", "prompt": "p",
        })
        self.assertEqual(201, deferred.status)
        self.assertEqual(2, self.service.reload_count)

    def test_skill_evolution_routes(self):
        self.service.skill_evolution.decision = "activated"
        auto = self.call("/v1/skill-evolution/auto", "POST", {"skill_name": "evolved"})
        self.assertEqual(201, auto.status)
        self.assertEqual(1, self.service.reload_count)

        artifact = {"name": "evolved", "skill_md": "md", "supporting_files": {}}
        proposed = self.call("/v1/skill-evolution/propose", "POST", {
            "skill_name": "evolved", "artifact": artifact,
        })
        self.assertEqual(201, proposed.status)
        self.assertEqual(artifact, self.service.skill_evolution.calls[-1][2])
        self.assertEqual(2, self.service.reload_count)

        legacy = self.call("/v1/skill-evolution/propose", "POST", {
            "skill_name": "evolved", "skill_md": "md",
        })
        self.assertEqual(201, legacy.status)
        self.assertEqual("md", self.service.skill_evolution.calls[-1][2]["skill_md"])
        self.assertEqual(3, self.service.reload_count)

        activated = self.call("/v1/skill-evolution/evolved/versions/1/activate", "POST")
        self.assertEqual(200, activated.status)
        self.assertEqual(4, self.service.reload_count)
        missing = self.call("/v1/skill-evolution/evolved/versions/2/activate", "POST")
        self.assertEqual(404, missing.status)

        rolled = self.call("/v1/skills/llm-review/versions/1/activate", "POST")
        self.assertEqual(200, rolled.status)
        self.assertEqual(5, self.service.reload_count)
        missing_roll = self.call("/v1/skills/llm-review/versions/2/activate", "POST")
        self.assertEqual(404, missing_roll.status)

    def test_alert_resolution_route(self):
        response = self.call("/api/alerts/1/resolve", "POST")
        self.assertEqual(200, response.status)
        self.assertEqual({"resolved": True}, response.body)
        self.assertEqual(404, self.call("/api/alerts/2/resolve", "POST").status)
        self.assertTrue(any(
            event[2] == "alert.resolve" for event in self.service.store.audit_events
        ))

    def test_tenant_quota_routes(self):
        status = self.call("/v1/tenant-quota")
        self.assertEqual(200, status.status)
        self.assertEqual(10, status.body["limits"]["daily_reviews"])

        created = self.call("/v1/tenant-quota", "POST", {
            "daily_reviews": 20, "daily_tokens": 5000,
            "daily_cost_usd": 2.5,
        })
        self.assertEqual(201, created.status)
        self.assertEqual(
            ("default", 20, 5000, 2.5), self.service.quota.calls[-1],
        )
        self.assertEqual(400, self.call("/v1/tenant-quota", "POST", {
            "daily_reviews": -1, "daily_tokens": 0, "daily_cost_usd": 0,
        }).status)
        self.assertEqual(400, self.call("/v1/tenant-quota", "POST", {
            "daily_reviews": 0, "daily_tokens": 0, "daily_cost_usd": "x",
        }).status)

    def test_quota_exceeded_maps_to_429(self):
        def exceeded(*args, **kwargs):
            from evoagent.errors import QuotaExceeded
            raise QuotaExceeded("quota exhausted")

        self.service.create_review = exceeded
        response = self.call("/v1/reviews", "POST", {
            "repository": "org/repo", "diff": "x",
        })
        self.assertEqual(429, response.status)

    def test_tenant_and_platform_administration_routes(self):
        created = self.call("/v1/tenants", "POST", {"tenant_id": "new-tenant"})
        self.assertEqual(201, created.status)
        self.assertTrue(created.body["created"])
        self.assertEqual(409, self.call(
            "/v1/tenants", "POST", {"tenant_id": "default"},
        ).status)
        self.assertEqual(400, self.call(
            "/v1/tenants", "POST", {"tenant_id": "../bad"},
        ).status)

        platform = self.call(
            "/v1/platform/users/alice/platform-admin", "POST", {"active": True},
        )
        self.assertEqual(200, platform.status)
        self.assertEqual(
            404, self.call(
                "/v1/platform/users/missing/platform-admin", "POST",
                {"active": False},
            ).status,
        )
        self.assertEqual(400, self.call(
            "/v1/platform/users/local-development/platform-admin", "POST",
            {"active": False},
        ).status)

        user = self.call("/v1/users", "POST", {
            "username": "bob", "password": "long-enough-password",
            "role": "maintainer", "tenant_id": "tenant-a",
        })
        self.assertEqual(201, user.status)
        self.assertEqual("tenant-a", self.service.auth.calls[-1][3])

        non_platform = Principal(
            "u", "tenant-admin", "default", "admin", False,
        )
        with patch.object(ApiHandler, "_principal",
                          lambda self, permission="read": non_platform):
            self.assertEqual(403, self.call("/v1/tenants").status)
            self.assertEqual(403, self.call("/v1/users", "POST", {
                "username": "bob", "password": "long-enough-password",
                "role": "maintainer", "tenant_id": "tenant-a",
            }).status)

    def test_api_token_issuance(self):
        secure = replace(self.settings, auth_required=True)

        def post(payload):
            response = Handler(
                self.service, secure, "/v1/auth/token",
                body=json.dumps(payload).encode("utf-8"),
            )
            response.do_POST()
            return response

        created = post({})
        self.assertEqual(201, created.status)
        self.assertEqual("issued-token", created.body["access_token"])
        self.assertEqual("default", created.body["tenant_id"])
        self.assertEqual(
            ("token", "admin", "default", None), self.service.auth.calls[-1],
        )
        self.assertEqual(403, post({"tenant_id": "other"}).status)
        self.assertEqual(400, post({"ttl_seconds": "x"}).status)
        self.assertTrue(any(
            event[2] == "auth.token.issue" for event in self.service.store.audit_events
        ))

    def test_user_management_routes(self):
        created = self.call("/v1/users", "POST", {
            "username": "alice", "password": "long-enough-password",
            "role": "maintainer",
        })
        self.assertEqual(201, created.status)
        self.assertEqual("alice", created.body["username"])
        self.assertEqual(
            ("create", "alice", "long-enough-password", "default", "maintainer"),
            self.service.auth.calls[-1],
        )

        self.assertEqual(200, self.call(
            "/v1/users/alice/password", "POST",
            {"password": "another-long-password"},
        ).status)
        self.assertEqual(404, self.call(
            "/v1/users/bob/password", "POST",
            {"password": "another-long-password"},
        ).status)
        self.assertEqual(200, self.call(
            "/v1/users/alice/active", "POST", {"active": False},
        ).status)
        self.assertEqual(400, self.call(
            "/v1/users/alice/active", "POST", {"active": "no"},
        ).status)
        self.assertEqual(400, self.call(
            "/v1/users/local-development/active", "POST", {"active": False},
        ).status)
        self.assertTrue(any(
            event[2] == "user.create" for event in self.service.store.audit_events
        ))

    def test_unknown_post_route_returns_404(self):
        self.assertEqual(404, self.call("/v1/unknown", "POST", {}).status)

    def test_unhandled_errors_return_500(self):
        def explode(*args, **kwargs):
            raise RuntimeError("boom")

        self.service.create_review = explode
        response = self.call("/v1/reviews", "POST", {"repository": "r", "diff": "d"})
        self.assertEqual(500, response.status)
        self.assertEqual({"error": "operation failed"}, response.body)


class DeleteRouteTests(ApiRouteTestCase):
    def test_revoke_requires_a_repository_and_reports_missing(self):
        self.assertEqual(400, self.call("/v1/repository-grants", "DELETE").status)
        self.assertEqual(404, self.call(
            "/v1/repository-grants?repository=other/repo", "DELETE",
        ).status)
        response = self.call(
            "/v1/repository-grants?repository=org/repo", "DELETE",
        )
        self.assertEqual(200, response.status)
        self.assertEqual({"revoked": True}, response.body)

    def test_unknown_delete_returns_404(self):
        self.assertEqual(404, self.call("/v1/unknown", "DELETE").status)


class HelperTests(unittest.TestCase):
    def test_body_optional_routes(self):
        for path in (
            "/v1/tasks/abcdef01/comment",
            "/v1/tasks/abcdef01/cancel",
            "/v1/tasks/abcdef01/resume",
            "/v1/skills/reload",
        ):
            self.assertTrue(body_optional(path), msg=path)
        self.assertFalse(body_optional("/v1/reviews"))

    def test_text_field_treats_null_as_absent(self):
        self.assertEqual("", text_field({"mode": None}, "mode"))
        self.assertEqual("agentic", text_field({"mode": "agentic"}, "mode"))
        self.assertEqual("3", text_field({"mode": 3}, "mode"))


if __name__ == "__main__":
    unittest.main()
