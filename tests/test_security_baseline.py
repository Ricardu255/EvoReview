"""End-to-end security baseline over real HTTP.

Implements the cross-tenant negative baseline promised by
``docs/CODE_REVIEW_GUIDELINES.zh-CN.md`` §9.2. Unlike the handler-level
tests (which use a fake service) every request here travels through the
real socket -> ApiHandler -> ReviewService -> TaskStore stack, on an
ephemeral port with a real SQLite store and auth enabled.

Each test maps to a concrete attack path that must stay rejected.
"""
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import uuid

from evoagent.adapters.auth import hash_password
from evoagent.api import ApiHandler, server_class
from evoagent.config import Settings
from evoagent.domain.models import ReviewReport, TaskState, TraceEvent
from evoagent.service import ReviewService
from evoagent.time_utils import utc_now


def _quiet_log(self, fmt, *args):
    pass


class SecurityBaselineE2E(unittest.TestCase):
    """Tenant-isolation attack paths, driven end to end over HTTP."""

    ALICE_PW = "AlicePassword1"
    MALLORY_PW = "MalloryPass12"
    BOB_PW = "BobPassword12"

    @classmethod
    def setUpClass(cls):
        handle, cls.db_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        cls.settings = Settings(
            host="127.0.0.1", port=8080, db_path=cls.db_path,
            max_diff_bytes=10000, max_steps=8, timeout_seconds=10,
            llm_base_url="", llm_api_key="", llm_model="",
            github_webhook_secret="whsec-baseline", github_token="",
            auto_post_review=False,
            auth_required=True,
            auth_secret="baseline-secret-" + "0123456789abcdef" * 2,
            bootstrap_admin_username="root",
            bootstrap_admin_password="RootPassword9",
            bootstrap_admin_platform_admin=True,
        )
        cls.service = ReviewService(cls.settings)
        handler = type("ConfiguredApiHandler", (ApiHandler,), {
            "service": cls.service, "settings": cls.settings,
            "log_message": _quiet_log,
        })
        handler.timeout = 30
        cls.server = server_class("127.0.0.1")(("127.0.0.1", 0), handler)
        cls.server.daemon_threads = True
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

        store = cls.service.store
        store.create_tenant("tenant-a")
        store.create_tenant("tenant-b")
        store.create_user("u-alice", "alice", hash_password(cls.ALICE_PW),
                          "tenant-a", "admin")
        store.create_user("u-mallory", "mallory", hash_password(cls.MALLORY_PW),
                          "tenant-a", "maintainer")
        store.create_user("u-bob", "bob", hash_password(cls.BOB_PW),
                          "tenant-b", "admin")
        cls.task_a = str(uuid.uuid4())
        store.create(cls.task_a, "org/a", 1, {}, "tenant-a")
        store.succeed(cls.task_a, ReviewReport(
            repository="org/a", pull_request=1, summary="reviewed", risk="low",
        ), TraceEvent(1, TaskState.SUCCESS, "done", utc_now()))
        cls.task_b = str(uuid.uuid4())
        store.create(cls.task_b, "org/b", 2, {}, "tenant-b")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.service.close()
        os.unlink(cls.db_path)

    # ---------- helpers ----------

    def _request(self, method, path, token=None, payload=None, headers=None):
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        hdrs = {"Content-Type": "application/json"} if data else {}
        if token:
            hdrs["Authorization"] = "Bearer " + token
        if headers:
            hdrs.update(headers)
        request = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                body = response.read().decode("utf-8")
                return response.status, json.loads(body or "{}")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8")
            try:
                parsed = json.loads(raw or "{}")
            except ValueError:
                parsed = {}
            return exc.code, parsed

    def _login(self, username, password, tenant_id):
        status, body = self._request("POST", "/v1/auth/login", payload={
            "username": username, "password": password, "tenant_id": tenant_id,
        })
        self.assertEqual(200, status, "login failed for %s: %s" % (username, body))
        return body["access_token"]

    def _alice(self):
        return self._login("alice", self.ALICE_PW, "tenant-a")

    def _mallory(self):
        return self._login("mallory", self.MALLORY_PW, "tenant-a")

    # ---------- attack paths ----------

    def test_anonymous_requests_are_rejected_when_auth_required(self):
        status, _ = self._request("GET", "/v1/users")
        self.assertEqual(401, status)

    def test_user_listing_is_tenant_scoped(self):
        status, body = self._request("GET", "/v1/users", token=self._alice())
        self.assertEqual(200, status)
        names = {u["username"] for u in body["users"]}
        self.assertIn("alice", names)
        self.assertNotIn("bob", names, "tenant-a listing leaked tenant-b user")

    def test_cross_tenant_username_adoption_is_rejected(self):
        status, body = self._request("POST", "/v1/users", token=self._alice(),
                                     payload={"username": "bob",
                                              "password": "Whatever123x",
                                              "role": "auditor"})
        self.assertEqual(400, status, body)

    def test_cross_tenant_password_reset_is_rejected(self):
        status, body = self._request("POST", "/v1/users/bob/password",
                                     token=self._alice(),
                                     payload={"password": "HijackPass1x"})
        self.assertEqual(404, status, body)

    def test_tenant_admin_cannot_issue_a_token_for_another_user(self):
        status, body = self._request("POST", "/v1/auth/token", token=self._alice(),
                                     payload={"username": "mallory"})
        self.assertEqual(403, status, body)

    def test_resetting_a_tenant_platform_admin_requires_platform_scope(self):
        # 专门造一个属于 tenant-a 的平台管理员：租户 admin 重置它必须 403，
        # 因为密码/平台标记都在全局 users 行上，重置即获得平台身份。
        store = self.service.store
        store.create_user("u-roota", "roota", hash_password("RootaPassword1"),
                          "tenant-a", "admin", platform_admin=True)
        status, body = self._request("POST", "/v1/users/roota/password",
                                     token=self._alice(),
                                     payload={"password": "PlatformPw1x"})
        self.assertEqual(403, status, body)

    def test_resetting_a_foreign_platform_admin_answers_404_not_403(self):
        # root 属于 default 租户而非 tenant-a：无论它是否是平台管理员都必须
        # 与不存在账号同样回答 404，避免该端点变成用户名/平台身份的探测 oracle。
        status, body = self._request("POST", "/v1/users/root/password",
                                     token=self._alice(),
                                     payload={"password": "PlatformPw1x"})
        self.assertEqual(404, status, body)

    def test_fix_requires_an_explicit_auto_fix_grant(self):
        # No grants exist for tenant-a at all: the fail-closed default must
        # reject the repair even for a user holding the fix permission.
        status, body = self._request("POST", "/v1/tasks/%s/fix" % self.task_a,
                                     token=self._mallory(), payload={})
        self.assertEqual(403, status, body)

    def test_tasks_are_invisible_across_tenants(self):
        status, _ = self._request("GET", "/v1/tasks/%s" % self.task_b,
                                  token=self._alice())
        self.assertEqual(404, status)
        # positive control: the owning tenant still sees its task
        status, _ = self._request("GET", "/v1/tasks/%s" % self.task_b,
                                  token=self._login("bob", self.BOB_PW, "tenant-b"))
        self.assertEqual(200, status)

    def test_login_against_the_wrong_tenant_is_rejected(self):
        status, _ = self._request("POST", "/v1/auth/login", payload={
            "username": "alice", "password": self.ALICE_PW,
            "tenant_id": "tenant-b",
        })
        self.assertEqual(401, status)

    def test_webhook_signature_is_enforced_end_to_end(self):
        event = {"X-GitHub-Event": "pull_request",
                 "Content-Type": "application/json"}
        status, _ = self._request("POST", "/webhooks/github",
                                  payload={"action": "opened"}, headers=event)
        self.assertEqual(401, status, "webhook without signature was accepted")
        status, _ = self._request("POST", "/webhooks/github",
                                  payload={"action": "opened"},
                                  headers=dict(event,
                                               **{"X-Hub-Signature-256": "sha256=deadbeef"}))
        self.assertEqual(401, status, "webhook with forged signature was accepted")


if __name__ == "__main__":
    unittest.main()
