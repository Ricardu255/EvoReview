"""Unit tests for the GitHub HTTP client and App authenticator.

All network access is mocked; these tests pin request construction, retry and
rate-limit behaviour, response size limits and pagination/comment handling.
"""
import io
import json
import sys
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

from evoagent.adapters.github import (
    GitHubAppAuthenticator, GitHubClient, verify_signature,
)


class FakeResponse:
    def __init__(self, body=b"{}", headers=None):
        self._body = body
        self.headers = headers or {}

    def read(self, size=-1):
        if size is None or size < 0:
            return self._body
        return self._body[:size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(code, detail=b"failure", headers=None):
    return urllib.error.HTTPError(
        "https://api.github.test/x", code, "error", headers or {}, io.BytesIO(detail),
    )


class HeaderAndSignatureTests(unittest.TestCase):
    def test_headers_include_version_and_optional_bearer_token(self):
        anonymous = GitHubClient("")._headers()
        self.assertNotIn("Authorization", anonymous)
        self.assertEqual("2022-11-28", anonymous["X-GitHub-Api-Version"])
        self.assertTrue(anonymous["User-Agent"].startswith("EvoReview/"))

        authorized = GitHubClient("tok")._headers("application/vnd.custom")
        self.assertEqual("Bearer tok", authorized["Authorization"])
        self.assertEqual("application/vnd.custom", authorized["Accept"])

    def test_signature_requires_a_prefixed_sha256_digest(self):
        body = b'{"action":"opened"}'
        import hashlib
        import hmac
        digest = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
        self.assertTrue(verify_signature("secret", body, "sha256=" + digest))
        self.assertFalse(verify_signature("secret", body, digest))
        self.assertFalse(verify_signature("", body, "sha256=" + digest))


class RequestTests(unittest.TestCase):
    def setUp(self):
        self.client = GitHubClient("token", timeout=3, max_attempts=3)

    def _capture(self):
        requests = []

        def fake_urlopen(request, timeout=None):
            requests.append((request, timeout))
            return FakeResponse(b'{"ok": true}')

        return requests, fake_urlopen

    def test_json_request_construction(self):
        requests, fake = self._capture()
        with patch("evoagent.adapters.github.urllib.request.urlopen", side_effect=fake):
            result = self.client._json("POST", "https://api.github.test/x", {"a": 1})
        self.assertEqual({"ok": True}, result)
        request, timeout = requests[0]
        self.assertEqual("POST", request.get_method())
        self.assertEqual(3, timeout)
        self.assertEqual(b'{"a": 1}', request.data)
        self.assertEqual("Bearer token", request.get_header("Authorization"))
        self.assertEqual("application/json", request.get_header("Content-type"))

    def test_empty_response_body_becomes_an_empty_object(self):
        with patch("evoagent.adapters.github.urllib.request.urlopen",
                   return_value=FakeResponse(b"")):
            self.assertEqual({}, self.client._json("GET", "https://api.github.test/x"))

    def test_fetch_diff_decodes_with_replacement(self):
        _, fake = self._capture()
        with patch("evoagent.adapters.github.urllib.request.urlopen", side_effect=fake):
            body = self.client._request("GET", "https://api.github.test/d", raw=True)
        self.assertEqual(b'{"ok": true}', body)

        with patch("evoagent.adapters.github.urllib.request.urlopen",
                   return_value=FakeResponse(b"diff \xff")):
            self.assertEqual("diff �", self.client.fetch_diff("https://api.github.test/d"))

    def test_post_comment_sends_markdown(self):
        requests, fake = self._capture()
        with patch("evoagent.adapters.github.urllib.request.urlopen", side_effect=fake):
            self.client.post_comment("https://api.github.test/repos/o/r/issues/1/", "hello")
        request = requests[0][0]
        self.assertEqual("https://api.github.test/repos/o/r/issues/1/comments", request.full_url)
        self.assertEqual({"body": "hello"}, json.loads(request.data.decode("utf-8")))

    def test_upsert_comment_updates_by_marker_or_posts_once(self):
        calls = []

        def fake_json(method, url, payload=None):
            calls.append((method, url, payload))
            if method == "GET":
                return [
                    {"body": "unrelated", "url": "https://api.test/1"},
                    {"body": "<!-- marker -->\nold", "url": "https://api.test/2"},
                ]
            return {}

        with patch.object(self.client, "_json", side_effect=fake_json):
            self.client.upsert_comment("https://api.test/issues/1", "new", "<!-- marker -->")
        self.assertEqual("GET", calls[0][0])
        self.assertEqual("https://api.test/issues/1/comments?per_page=100", calls[0][1])
        self.assertEqual(("PATCH", "https://api.test/2",
                          {"body": "<!-- marker -->\nnew"}), calls[1])

        calls.clear()

        def fake_post(method, url, payload=None):
            calls.append((method, url, payload))
            return [] if method == "GET" else {}

        with patch.object(self.client, "_json", side_effect=fake_post):
            self.client.upsert_comment("https://api.test/issues/1", "new", "<!-- marker -->")
        self.assertEqual(("POST", "https://api.test/issues/1/comments",
                          {"body": "<!-- marker -->\nnew"}), calls[1])


class RetryTests(unittest.TestCase):
    def setUp(self):
        self.client = GitHubClient("token", timeout=3, max_attempts=4)

    def test_retryable_http_errors_are_retried_with_backoff(self):
        responses = [
            http_error(500),
            http_error(502),
            FakeResponse(b'{"ok": true}'),
        ]
        with patch("evoagent.adapters.github.urllib.request.urlopen",
                   side_effect=responses), \
                patch("evoagent.adapters.github.time.sleep") as sleep, \
                patch("evoagent.adapters.github.random.random", return_value=0.0):
            self.assertEqual({"ok": True}, self.client._json("GET", "https://api.test/x"))
        self.assertEqual([1, 2], [call.args[0] for call in sleep.call_args_list])

    def test_retry_after_and_rate_limit_reset_control_the_delay(self):
        with patch("evoagent.adapters.github.urllib.request.urlopen",
                   side_effect=[http_error(429, headers={"Retry-After": "7"}),
                                FakeResponse(b"{}")]), \
                patch("evoagent.adapters.github.time.sleep") as sleep:
            self.client._json("GET", "https://api.test/x")
        self.assertEqual(7.0, sleep.call_args_list[0].args[0])

        with patch("evoagent.adapters.github.urllib.request.urlopen",
                   side_effect=[http_error(403, headers={
                       "X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1100",
                   }), FakeResponse(b"{}")]), \
                patch("evoagent.adapters.github.time.sleep") as sleep, \
                patch("evoagent.adapters.github.time.time", return_value=1000.0):
            self.client._json("GET", "https://api.test/x")
        self.assertEqual(30.0, sleep.call_args_list[0].args[0])  # capped at 30s

    def test_invalid_retry_headers_fall_back_to_exponential_backoff(self):
        with patch("evoagent.adapters.github.urllib.request.urlopen",
                   side_effect=[http_error(503, headers={
                       "Retry-After": "soon", "X-RateLimit-Reset": "later",
                   }), FakeResponse(b"{}")]), \
                patch("evoagent.adapters.github.time.sleep") as sleep, \
                patch("evoagent.adapters.github.random.random", return_value=0.0):
            self.client._json("GET", "https://api.test/x")
        self.assertEqual(1.0, sleep.call_args_list[0].args[0])

    def test_non_retryable_status_raises_immediately(self):
        for code in (400, 401, 403, 404, 422):
            with self.subTest(code=code), patch(
                "evoagent.adapters.github.urllib.request.urlopen",
                side_effect=http_error(code, detail=b"bad request"),
            ), patch("evoagent.adapters.github.time.sleep") as sleep:
                with self.assertRaisesRegex(RuntimeError, "HTTP %d" % code):
                    self.client._json("GET", "https://api.test/x")
            sleep.assert_not_called()

    def test_exhausted_retries_raise_with_the_last_error(self):
        with patch("evoagent.adapters.github.urllib.request.urlopen",
                   side_effect=[http_error(500) for _ in range(4)]), \
                patch("evoagent.adapters.github.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "HTTP 500"):
                self.client._json("GET", "https://api.test/x")

    def test_url_errors_and_timeouts_are_retried_then_raised(self):
        with patch("evoagent.adapters.github.urllib.request.urlopen",
                   side_effect=[urllib.error.URLError("down"),
                                TimeoutError("slow"),
                                FakeResponse(b"{}")]), \
                patch("evoagent.adapters.github.time.sleep"):
            self.assertEqual({}, self.client._json("GET", "https://api.test/x"))

        with patch("evoagent.adapters.github.urllib.request.urlopen",
                   side_effect=urllib.error.URLError("down")), \
                patch("evoagent.adapters.github.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "request failed"):
                self.client._json("GET", "https://api.test/x")


class ResponseLimitTests(unittest.TestCase):
    def test_declared_content_length_limit(self):
        client = GitHubClient("", max_response_bytes=10)
        with patch("evoagent.adapters.github.urllib.request.urlopen",
                   return_value=FakeResponse(b"{}", {"Content-Length": "11"})):
            with self.assertRaisesRegex(RuntimeError, "exceeds the 10 byte limit"):
                client._json("GET", "https://api.test/x")

    def test_invalid_content_length_is_ignored(self):
        client = GitHubClient("", max_response_bytes=10)
        with patch("evoagent.adapters.github.urllib.request.urlopen",
                   return_value=FakeResponse(b"{}", {"Content-Length": "unknown"})):
            self.assertEqual({}, client._json("GET", "https://api.test/x"))

    def test_streamed_body_limit(self):
        client = GitHubClient("", max_response_bytes=10)
        with patch("evoagent.adapters.github.urllib.request.urlopen",
                   return_value=FakeResponse(b"x" * 11)):
            with self.assertRaisesRegex(RuntimeError, "exceeds the 10 byte limit"):
                client._json("GET", "https://api.test/x")


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.client = GitHubClient("token")

    def test_get_file_decodes_base64_content_and_quotes_paths(self):
        calls = []

        def fake_json(method, url, payload=None):
            calls.append((method, url))
            import base64
            return {"content": base64.b64encode(b"hello\n").decode("ascii")}

        with patch.object(self.client, "_json", side_effect=fake_json):
            result = self.client.get_file("org/repo", "src/a b.py", "feature/x")
        self.assertEqual("hello\n", result["decoded_content"])
        self.assertIn("src/a%20b.py", calls[0][1])
        self.assertIn("ref=feature%2Fx", calls[0][1])

    def test_get_pull_request_and_repository_urls(self):
        urls = []

        def fake_json(method, url, payload=None):
            urls.append(url)
            return {}

        with patch.object(self.client, "_json", side_effect=fake_json):
            self.client.get_pull_request("org/repo", 12)
            self.client.get_repository("org/repo")
        self.assertEqual([
            "https://api.github.com/repos/org/repo/pulls/12",
            "https://api.github.com/repos/org/repo",
        ], urls)

    def test_ensure_repository_access_checks_full_name_case_insensitively(self):
        with patch.object(self.client, "get_repository",
                          return_value={"full_name": "Org/Repo"}):
            self.client.ensure_repository_access("org/repo")

        for value in ({}, {"full_name": "other/repo"}):
            with self.subTest(value=value), patch.object(
                self.client, "get_repository", return_value=value,
            ):
                with self.assertRaises(PermissionError):
                    self.client.ensure_repository_access("org/repo")

    def test_branch_and_file_commit_payloads(self):
        calls = []

        def fake_json(method, url, payload=None):
            calls.append((method, url, payload))
            return {}

        with patch.object(self.client, "_json", side_effect=fake_json):
            self.client.create_branch("org/repo", "evo/fix", "sha-1")
            self.client.commit_file(
                "org/repo", "src/a b.py", "evo/fix", "content\n", "file-sha", "msg",
            )
        self.assertEqual({
            "ref": "refs/heads/evo/fix", "sha": "sha-1",
        }, calls[0][2])
        self.assertEqual("PUT", calls[1][0])
        self.assertIn("src/a%20b.py", calls[1][1])
        self.assertEqual("file-sha", calls[1][2]["sha"])
        self.assertEqual("evo/fix", calls[1][2]["branch"])
        import base64
        self.assertEqual(b"content\n", base64.b64decode(calls[1][2]["content"]))

    def test_atomic_commit_builds_a_tree_then_a_branch(self):
        calls = []

        def fake_json(method, url, payload=None):
            calls.append((method, url, payload))
            if url.endswith("/git/commits/parent-sha"):
                return {"tree": {"sha": "base-tree"}}
            if url.endswith("/git/trees"):
                return {"sha": "new-tree"}
            if url.endswith("/git/commits"):
                return {"sha": "new-commit"}
            return {}

        with patch.object(self.client, "_json", side_effect=fake_json):
            result = self.client.create_atomic_commit(
                "org/repo", "evo/fix", "parent-sha",
                {"b.py": "b\n", "a.py": "a\n"}, "message",
            )
        self.assertEqual("new-commit", result["sha"])
        tree_payload = calls[1][2]
        self.assertEqual("base-tree", tree_payload["base_tree"])
        self.assertEqual(["a.py", "b.py"], [item["path"] for item in tree_payload["tree"]])
        self.assertEqual(["parent-sha"], calls[2][2]["parents"])
        self.assertEqual("new-tree", calls[2][2]["tree"])
        self.assertEqual("refs/heads/evo/fix", calls[3][2]["ref"])

    def test_draft_pull_request_and_archive_download(self):
        with patch.object(self.client, "_json", return_value={"number": 4}) as call:
            self.assertEqual({"number": 4}, self.client.create_draft_pull_request(
                "org/repo", "title", "head", "main", "body",
            ))
        self.assertTrue(call.call_args.args[2]["draft"])

        with patch.object(self.client, "_request",
                          return_value=b"zip") as request:
            self.assertEqual(b"zip", self.client.download_archive("org/repo", "feature/x"))
        self.assertEqual("application/vnd.github+json", request.call_args.kwargs["accept"])
        self.assertTrue(request.call_args.kwargs["raw"])


class GitHubAppAuthenticatorTests(unittest.TestCase):
    def setUp(self):
        GitHubAppAuthenticator._cache.clear()

    def test_installation_token_is_cached_until_near_expiry(self):
        authenticator = GitHubAppAuthenticator("123", "unused")
        responses = [FakeResponse(
            json.dumps({"token": "tok-1", "expires_at": "2035-01-01T00:00:00Z"}).encode(),
        )]
        with patch.object(authenticator, "app_jwt", return_value="jwt"), \
                patch("evoagent.adapters.github.urllib.request.urlopen",
                      side_effect=responses) as urlopen:
            self.assertEqual("tok-1", authenticator.installation_token(99))
            self.assertEqual("tok-1", authenticator.installation_token(99))
        self.assertEqual(1, urlopen.call_count)
        request = urlopen.call_args.args[0]
        self.assertEqual("Bearer jwt", request.get_header("Authorization"))

    def test_expired_cache_entries_are_refreshed(self):
        authenticator = GitHubAppAuthenticator("123", "unused")
        GitHubAppAuthenticator._cache[("123", 7)] = {
            "token": "old", "expires_at": 0,
        }
        response = FakeResponse(json.dumps({
            "token": "new", "expires_at": "2035-01-01T00:00:00Z",
        }).encode())
        with patch.object(authenticator, "app_jwt", return_value="jwt"), \
                patch("evoagent.adapters.github.urllib.request.urlopen",
                      return_value=response):
            self.assertEqual("new", authenticator.installation_token(7))

    def test_invalid_expiry_falls_back_to_a_short_lived_cache(self):
        authenticator = GitHubAppAuthenticator("123", "unused")
        response = FakeResponse(json.dumps({
            "token": "tok", "expires_at": "not-a-date",
        }).encode())
        with patch.object(authenticator, "app_jwt", return_value="jwt"), \
                patch("evoagent.adapters.github.urllib.request.urlopen",
                      return_value=response):
            self.assertEqual("tok", authenticator.installation_token(8))
        self.assertGreater(
            GitHubAppAuthenticator._cache[("123", 8)]["expires_at"], 0,
        )

    def test_app_jwt_signs_the_app_claims_with_the_private_key(self):
        with tempfile.NamedTemporaryFile(delete=False) as handle:
            handle.write(b"pem-bytes")
            path = handle.name
        self.addCleanup(lambda: __import__("os").unlink(path))
        authenticator = GitHubAppAuthenticator("42", path)
        with patch("jwt.encode", return_value="signed") as encode:
            self.assertEqual("signed", authenticator.app_jwt())
        claims = encode.call_args.args[0]
        self.assertEqual("42", claims["iss"])
        self.assertEqual(600, claims["exp"] - claims["iat"])
        self.assertEqual("RS256", encode.call_args.kwargs["algorithm"])

    def test_missing_private_key_file_fails_loudly(self):
        authenticator = GitHubAppAuthenticator("42", "does-not-exist.pem")
        with self.assertRaises(FileNotFoundError):
            authenticator.app_jwt()

    def test_app_jwt_requires_the_jwt_extra(self):
        authenticator = GitHubAppAuthenticator("42", "unused")
        with patch.dict(sys.modules, {"jwt": None}):
            with self.assertRaisesRegex(RuntimeError, "PyJWT"):
                authenticator.app_jwt()


if __name__ == "__main__":
    unittest.main()
