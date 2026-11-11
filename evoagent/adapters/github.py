import hashlib
import hmac
import json
import urllib.error
import urllib.request
import urllib.parse
import base64
import random
import re
import threading
import time
from datetime import datetime
from typing import Dict

from .. import __version__


USER_AGENT = "EvoReview/" + __version__
REPOSITORY_NAME = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

# Webhook payloads carry URLs (diff_url, issue_url, archive_url, ...) that are
# attacker-influenced once the webhook secret is known.  The client holds a
# privileged GitHub token, so it must never become a generic HTTPS client.
GITHUB_HOSTS = frozenset({
    "api.github.com", "github.com", "codeload.github.com",
    "uploads.github.com", "raw.githubusercontent.com",
    "patch-diff.githubusercontent.com", "objects.githubusercontent.com",
})


def is_github_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(str(url))
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and not parsed.username and not parsed.password
        and port in (None, 443)
        and (parsed.hostname or "").lower() in GITHUB_HOSTS
    )


def verify_signature(secret: str, body: bytes, signature: str) -> bool:
    if not secret or not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


class GitHubClient:
    # Guard against unexpectedly large downloads (repository archives, diffs).
    MAX_RESPONSE_BYTES = 64 * 1024 * 1024

    def __init__(
        self, token: str, timeout: int = 30, max_attempts: int = 4,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
    ):
        self.token = token
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.max_response_bytes = max(1, int(max_response_bytes))

    def _headers(
        self, accept: str = "application/vnd.github+json", url: str = None,
    ) -> Dict[str, str]:
        headers = {"Accept": accept, "User-Agent": USER_AGENT, "X-GitHub-Api-Version": "2022-11-28"}
        # Never send credentials to a host outside the GitHub allowlist.  A
        # None url keeps the historical behaviour for callers that only need
        # the header set (and is covered by the header unit tests).
        if self.token and (url is None or is_github_url(url)):
            headers["Authorization"] = "Bearer " + self.token
        return headers

    @staticmethod
    def _validate_url(url: str) -> None:
        if not is_github_url(url):
            raise ValueError("refusing a request to a non-GitHub URL")

    def fetch_diff(self, url: str) -> str:
        body = self._request(
            "GET", url, accept="application/vnd.github.v3.diff", raw=True
        )
        return body.decode("utf-8", errors="replace")

    def create_check_run(
        self, repository: str, head_sha: str, conclusion: str,
        title: str, summary: str,
    ) -> dict:
        repository = self._validate_repository(repository)
        head_sha = str(head_sha or "").strip()
        if not head_sha:
            raise ValueError("head_sha is required for a check run")
        return self._json(
            "POST", "https://api.github.com/repos/%s/check-runs" % repository,
            {
                "name": "EvoReview", "head_sha": head_sha,
                "status": "completed", "conclusion": conclusion,
                "output": {"title": title[:255], "summary": summary[:65000]},
            },
        )

    def update_check_run(
        self, repository: str, check_run_id: int, conclusion: str,
        title: str, summary: str,
    ) -> dict:
        repository = self._validate_repository(repository)
        return self._json(
            "PATCH", "https://api.github.com/repos/%s/check-runs/%d"
            % (repository, int(check_run_id)),
            {
                "status": "completed", "conclusion": conclusion,
                "output": {"title": title[:255], "summary": summary[:65000]},
            },
        )

    def post_review(
        self, repository: str, number: int, body: str,
        comments: list, event: str = "COMMENT",
    ) -> dict:
        repository = self._validate_repository(repository)
        return self._json(
            "POST", "https://api.github.com/repos/%s/pulls/%d/reviews"
            % (repository, int(number)),
            {"body": body, "event": event, "comments": list(comments)},
        )

    def post_comment(self, api_url: str, markdown: str) -> None:
        url = api_url.rstrip("/") + "/comments"
        self._validate_url(url)
        request = urllib.request.Request(
            url,
            data=json.dumps({"body": markdown}).encode("utf-8"),
            headers=dict(self._headers(url=url), **{"Content-Type": "application/json"}),
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            final_url = getattr(response, "geturl", None)
            if callable(final_url):
                final_url = final_url()
                if final_url and not is_github_url(final_url):
                    raise RuntimeError("GitHub API redirected to a non-GitHub host")
            return None

    def upsert_comment(self, api_url: str, markdown: str, marker: str) -> None:
        """Update this service's existing review comment instead of creating duplicates."""
        comments_url = api_url.rstrip("/") + "/comments"
        comments = self._json("GET", comments_url + "?per_page=100")
        if not isinstance(comments, list):
            comments = []
        page = 2
        while len(comments) >= 100 and page <= 10:
            batch = self._json(
                "GET", comments_url + "?per_page=100&page=%d" % page,
            )
            if not isinstance(batch, list) or not batch:
                break
            comments.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        body = marker + "\n" + markdown
        for comment in comments:
            if marker in str(comment.get("body", "")):
                self._json("PATCH", comment["url"], {"body": body})
                return
        self._json("POST", comments_url, {"body": body})

    def _json(self, method: str, url: str, payload=None):
        return self._request(method, url, payload)

    def _request(
        self, method: str, url: str, payload=None,
        accept: str = "application/vnd.github+json", raw: bool = False,
    ):
        self._validate_url(url)
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        for attempt in range(1, self.max_attempts + 1):
            request = urllib.request.Request(
                url, data=data,
                headers=dict(self._headers(accept, url), **{"Content-Type": "application/json"}),
                method=method,
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    # A 30x to another host would otherwise bypass the check
                    # above while urllib still forwards our credentials.
                    final_url = getattr(response, "geturl", None)
                    if callable(final_url):
                        final_url = final_url()
                        if final_url and not is_github_url(final_url):
                            raise RuntimeError(
                                "GitHub API redirected to a non-GitHub host"
                            )
                    declared = response.headers.get("Content-Length")
                    try:
                        too_large = bool(declared) and int(declared) > self.max_response_bytes
                    except (TypeError, ValueError):
                        too_large = False
                    if too_large:
                        raise RuntimeError(
                            "GitHub API response exceeds the %d byte limit"
                            % self.max_response_bytes
                        )
                    body = response.read(self.max_response_bytes + 1)
                    if len(body) > self.max_response_bytes:
                        raise RuntimeError(
                            "GitHub API response exceeds the %d byte limit"
                            % self.max_response_bytes
                        )
                    if raw:
                        return body
                    return json.loads(body.decode("utf-8")) if body else {}
            except urllib.error.HTTPError as exc:
                retryable = exc.code in {429, 500, 502, 503, 504}
                if exc.code == 403 and exc.headers.get("X-RateLimit-Remaining") == "0":
                    retryable = True
                if not retryable or attempt >= self.max_attempts:
                    detail = exc.read(1000).decode("utf-8", errors="replace")
                    raise RuntimeError(
                        "GitHub API %s %s returned HTTP %d: %s"
                        % (method, url, exc.code, detail)
                    ) from exc
                retry_after = exc.headers.get("Retry-After")
                reset = exc.headers.get("X-RateLimit-Reset")
                delay = None
                if retry_after:
                    try:
                        delay = float(retry_after)
                    except (TypeError, ValueError):
                        delay = None
                if delay is None and reset:
                    try:
                        delay = max(0.0, float(reset) - time.time())
                    except (TypeError, ValueError):
                        delay = None
                if delay is None:
                    delay = min(2 ** (attempt - 1) + random.random(), 10)
                time.sleep(min(delay, 30))
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt >= self.max_attempts:
                    raise RuntimeError("GitHub API request failed: %s" % exc) from exc
                time.sleep(min(2 ** (attempt - 1) + random.random(), 10))

    def get_pull_request(self, repository: str, number: int) -> dict:
        repository = self._validate_repository(repository)
        return self._json("GET", "https://api.github.com/repos/%s/pulls/%d" % (repository, number))

    def get_file(self, repository: str, path: str, ref: str) -> dict:
        repository = self._validate_repository(repository)
        quoted = urllib.parse.quote(path, safe="/")
        result = self._json("GET", "https://api.github.com/repos/%s/contents/%s?ref=%s" % (
            repository, quoted, urllib.parse.quote(ref, safe="")
        ))
        result["decoded_content"] = base64.b64decode(result["content"]).decode("utf-8")
        return result

    def get_repository(self, repository: str) -> dict:
        repository = self._validate_repository(repository)
        return self._json("GET", "https://api.github.com/repos/%s" % repository)

    @staticmethod
    def _validate_repository(repository: str) -> str:
        value = str(repository or "")
        if not REPOSITORY_NAME.fullmatch(value):
            raise ValueError("repository must use the owner/name format")
        return value

    def ensure_repository_access(self, repository: str) -> None:
        result = self.get_repository(repository)
        if str(result.get("full_name", "")).lower() != repository.lower():
            raise PermissionError("GitHub installation is not authorized for this repository")

    def create_branch(self, repository: str, branch: str, sha: str) -> None:
        repository = self._validate_repository(repository)
        self._json("POST", "https://api.github.com/repos/%s/git/refs" % repository,
                   {"ref": "refs/heads/" + branch, "sha": sha})

    def commit_file(self, repository: str, path: str, branch: str, content: str, sha: str, message: str) -> dict:
        repository = self._validate_repository(repository)
        quoted = urllib.parse.quote(path, safe="/")
        return self._json("PUT", "https://api.github.com/repos/%s/contents/%s" % (repository, quoted), {
            "message": message, "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "sha": sha, "branch": branch,
        })

    def create_atomic_commit(
        self, repository: str, branch: str, parent_sha: str,
        files: Dict[str, str], message: str,
    ) -> dict:
        repository = self._validate_repository(repository)
        parent = self._json(
            "GET", "https://api.github.com/repos/%s/git/commits/%s"
            % (repository, parent_sha)
        )
        tree = self._json(
            "POST", "https://api.github.com/repos/%s/git/trees" % repository,
            {
                "base_tree": parent["tree"]["sha"],
                "tree": [
                    {"path": path, "mode": "100644", "type": "blob", "content": content}
                    for path, content in sorted(files.items())
                ],
            },
        )
        commit = self._json(
            "POST", "https://api.github.com/repos/%s/git/commits" % repository,
            {"message": message, "tree": tree["sha"], "parents": [parent_sha]},
        )
        self.create_branch(repository, branch, commit["sha"])
        return commit

    def create_draft_pull_request(
        self, repository: str, title: str, head: str, base: str, body: str,
    ) -> dict:
        repository = self._validate_repository(repository)
        return self._json(
            "POST", "https://api.github.com/repos/%s/pulls" % repository,
            {"title": title, "head": head, "base": base, "body": body, "draft": True},
        )

    def download_archive(self, repository: str, ref: str) -> bytes:
        repository = self._validate_repository(repository)
        return self._request(
            "GET", "https://api.github.com/repos/%s/zipball/%s"
            % (repository, urllib.parse.quote(ref, safe="")),
            accept="application/vnd.github+json", raw=True,
        )


class GitHubAppAuthenticator:
    _cache = {}
    _lock = threading.Lock()
    def __init__(self, app_id: str, private_key_path: str):
        self.app_id = app_id
        self.private_key_path = private_key_path

    def app_jwt(self) -> str:
        try:
            import jwt
        except ImportError as exc:
            raise RuntimeError("GitHub App mode requires: pip install PyJWT[crypto]") from exc
        with open(self.private_key_path, "rb") as handle:
            key = handle.read()
        now = int(time.time())
        return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": self.app_id}, key, algorithm="RS256")

    def installation_token(self, installation_id: int) -> str:
        cache_key = (self.app_id, int(installation_id))
        with self._lock:
            cached = self._cache.get(cache_key)
            if cached and cached["expires_at"] > time.time() + 120:
                return cached["token"]
        request = urllib.request.Request(
            "https://api.github.com/app/installations/%d/access_tokens" % installation_id,
            data=b"{}", method="POST",
            headers={"Authorization": "Bearer " + self.app_jwt(), "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28", "User-Agent": USER_AGENT,
                     "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
        expires = result.get("expires_at", "")
        try:
            expires_at = datetime.fromisoformat(expires.replace("Z", "+00:00")).timestamp()
        except ValueError:
            expires_at = time.time() + 3000
        with self._lock:
            self._cache[cache_key] = {"token": result["token"], "expires_at": expires_at}
        return result["token"]
