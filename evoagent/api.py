import hashlib
import hmac
import ipaddress
import json
import logging
import mimetypes
import os
import re
import secrets
import signal
import socket
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict

from . import __version__
from .config import Settings
from .adapters.auth import Principal
from .adapters.github import verify_signature
from .adapters.metrics import metrics
from .domain.modes import public_taxonomy, resolve_mode
from .domain.report import to_markdown
from .domain.sarif import to_sarif
from .errors import AuthenticationRequired, ModelNotConfigured, QuotaExceeded
from .openapi import build_openapi
from .service import ReviewService


TASK = re.compile(r"^/v1/tasks/([0-9a-f-]+)$")
REPORT = re.compile(r"^/v1/tasks/([0-9a-f-]+)/report$")
SARIF = re.compile(r"^/v1/tasks/([0-9a-f-]+)/sarif$")
FIX = re.compile(r"^/v1/tasks/([0-9a-f-]+)/fix$")
FEEDBACK = re.compile(r"^/v1/tasks/([0-9a-f-]+)/feedback$")
CANCEL = re.compile(r"^/v1/tasks/([0-9a-f-]+)/cancel$")
RESUME = re.compile(r"^/v1/tasks/([0-9a-f-]+)/resume$")
COMMENT = re.compile(r"^/v1/tasks/([0-9a-f-]+)/comment$")
ALERT_RESOLVE = re.compile(r"^/api/alerts/(\d+)/resolve$")
USERS = "/v1/users"
TENANTS = "/v1/tenants"
TENANT_QUOTA = "/v1/tenant-quota"
USER_PASSWORD = re.compile(r"^/v1/users/([^/]+)/password$")
USER_ACTIVE = re.compile(r"^/v1/users/([^/]+)/active$")
PLATFORM_ADMIN = re.compile(r"^/v1/platform/users/([^/]+)/platform-admin$")
TENANT_ID = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
GRANTS = "/v1/repository-grants"
ROLLBACK = re.compile(r"^/v1/skills/([A-Za-z0-9_-]+)/versions/(\d+)/activate$")
PROMPT_VERSIONS = re.compile(r"^/v1/skills/([A-Za-z0-9_-]+)/versions$")
SKILL_ARTIFACT_VERSIONS = re.compile(r"^/v1/skill-evolution/([a-z0-9_-]+)/versions$")
SKILL_ARTIFACT_ACTIVATE = re.compile(
    r"^/v1/skill-evolution/([a-z0-9_-]+)/versions/(\d+)/activate$"
)
WEB_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "web"))
GITHUB_SETUP_STATE_TTL_SECONDS = 600
TASK_STATES = {
    "PENDING", "PLANNING", "EXECUTING", "REVIEWING",
    "SUCCESS", "FAILED", "CANCELLED",
}
logger = logging.getLogger("evoagent")


def body_optional(path: str) -> bool:
    """POST routes that act on the URL alone and need no JSON body."""
    return bool(
        COMMENT.match(path) or CANCEL.match(path) or RESUME.match(path)
        or ALERT_RESOLVE.match(path)
        or path == "/v1/skills/reload"
    )


def text_field(payload: Dict[str, Any], name: str) -> str:
    """Read an optional string field, treating JSON null as absent."""
    value = payload.get(name)
    return "" if value is None else str(value)


class ApiHandler(BaseHTTPRequestHandler):
    service: ReviewService
    settings: Settings
    server_version = "EvoReview/" + __version__

    def log_message(self, fmt: str, *args: Any) -> None:
        # Setup state tokens travel in the query string; never write them to logs.
        safe = [
            re.sub(r"(state=)[^&\s\"]+", r"\1<redacted>", str(arg))
            for arg in args
        ]
        try:
            print("%s - %s" % (self.address_string(), fmt % tuple(safe)))
        except (TypeError, ValueError):
            print("%s - request" % self.address_string())

    def _headers(self, status: int, content_type: str, length: int) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self'; "
            "script-src 'self'; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        )
        self.end_headers()

    def _principal(self, permission: str = "read") -> Principal:
        if not self.settings.auth_required:
            return Principal(
                "local", "local-development", self.settings.default_tenant_id,
                "admin", True,
            )
        principal = self.service.auth.authenticate(self.headers.get("Authorization", ""))
        self.service.auth.require(principal, (permission,))
        return principal

    def _authenticate_or_send(self, permission: str = "read"):
        try:
            return self._principal(permission)
        except AuthenticationRequired as exc:
            self._send_json(401, {"error": str(exc)})
            return None
        except PermissionError as exc:
            self._send_json(403, {"error": str(exc)})
            return None

    def _client_key(self) -> str:
        """Best-effort client identity for login throttling.

        Tunnel and reverse-proxy deployments would otherwise put every user in
        the single ``127.0.0.1`` bucket.  ``X-Forwarded-For`` is honoured only
        when the socket peer is in ``EVOAGENT_TRUSTED_PROXIES``, and the
        rightmost untrusted hop is used so clients cannot spoof their way out
        of the limiter.
        """
        try:
            peer = str(self.client_address[0])
        except (IndexError, TypeError, AttributeError):
            return "unknown"
        networks = [
            ipaddress.ip_network(item, strict=False)
            for item in self.settings.trusted_proxies
        ]
        if not networks:
            return peer
        try:
            peer_address = ipaddress.ip_address(peer)
        except ValueError:
            return "unknown"
        if not any(peer_address in network for network in networks):
            return peer
        forwarded = self.headers.get("X-Forwarded-For", "")
        for hop in reversed([item.strip() for item in forwarded.split(",") if item.strip()]):
            try:
                candidate = ipaddress.ip_address(hop)
            except ValueError:
                continue
            if not any(candidate in network for network in networks):
                return str(candidate)
        return peer

    def _check_manageable_target(self, username: str, principal) -> None:
        """Reject tenant-scoped account changes that would cross tenant lines.

        The password, the active flag and platform scope live on the global
        user row, while ``manage`` is granted per tenant.  A tenant manager may
        therefore only touch accounts that belong to its own tenant and nowhere
        else; anything wider needs platform scope.
        """
        if principal.can("platform"):
            return
        target = self.service.store.get_user(username) or {}
        memberships = {
            str(item["tenant_id"]) for item in target.get("memberships", [])
        }
        if principal.tenant_id not in memberships:
            # The account is not a member here, so the tenant-scoped store call
            # answers 404.  Return without another error to avoid turning this
            # endpoint into a username/platform-admin oracle.
            return
        if target.get("platform_admin"):
            raise PermissionError(
                "only platform administrators may modify a platform administrator"
            )
        if memberships - {principal.tenant_id} and username != principal.username:
            raise PermissionError(
                "only platform administrators may modify an account that belongs "
                "to multiple tenants"
            )

    def _setup_state_hash(self, token: str) -> str:
        """Hash a setup token before persistence; tokens are never stored raw."""
        return hmac.new(
            self.settings.github_setup_state.encode("utf-8"),
            token.encode("utf-8"), hashlib.sha256,
        ).hexdigest()

    def _send_json(self, status: int, value: Dict[str, Any]) -> None:
        body = json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(body))
        self.wfile.write(body)

    def _send_text(self, status: int, text: str, content_type: str = "text/plain; charset=utf-8") -> None:
        body = text.encode("utf-8")
        self._headers(status, content_type, len(body))
        self.wfile.write(body)

    def _serve_file(self, filename: str) -> None:
        path = os.path.abspath(os.path.join(WEB_ROOT, filename))
        if not path.startswith(WEB_ROOT + os.sep) and path != WEB_ROOT:
            self._send_json(404, {"error": "not found"})
            return
        try:
            with open(path, "rb") as handle:
                body = handle.read()
        except OSError:
            self._send_json(404, {"error": "not found"})
            return
        content_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {"application/javascript", "application/json"}:
            content_type += "; charset=utf-8"
        self._headers(200, content_type, len(body))
        self.wfile.write(body)

    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("invalid Content-Length")
        limit = self.settings.max_diff_bytes + 256 * 1024
        if length <= 0 or length > limit:
            raise ValueError("request body is empty or too large")
        return self.rfile.read(length)

    @staticmethod
    def _read_json(body: bytes) -> Dict[str, Any]:
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("request body must be valid UTF-8 JSON")
        if not isinstance(value, dict):
            raise ValueError("JSON root must be an object")
        return value

    @staticmethod
    def _query_int(query: Dict[str, list], name: str, default: int) -> int:
        raw = query.get(name, [str(default)])[0]
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise ValueError("%s must be an integer" % name)
        if value <= 0:
            raise ValueError("%s must be positive" % name)
        return value

    def do_GET(self) -> None:
        try:
            self._handle_get()
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
        except AuthenticationRequired as exc:
            self._send_json(401, {"error": str(exc)})
        except PermissionError as exc:
            self._send_json(403, {"error": str(exc)})
        except Exception:
            metrics.inc("http_errors_total")
            logger.exception("unhandled GET failure for %s", self.path)
            self._send_json(500, {"error": "operation failed"})

    def _handle_get(self) -> None:
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path
        query = urllib.parse.parse_qs(parsed_url.query)
        if path == "/":
            self._serve_file("index.html")
            return
        if path == "/assets/app.css":
            self._serve_file("app.css")
            return
        if path == "/assets/login.css":
            self._serve_file("login.css")
            return
        if path == "/assets/app.js":
            self._serve_file("app.js")
            return
        if path == "/health":
            # Unauthenticated liveness probe only; deployment details live
            # behind authentication at /health/detail.
            self._send_json(200, {"status": "ok"})
            return
        if path == "/openapi.json":
            self._send_json(200, build_openapi())
            return
        if path == "/github/install":
            if not self.settings.github_app_slug or not self.settings.github_setup_state:
                self._send_json(503, {
                    "error": "GitHub App setup requires EVOAGENT_GITHUB_APP_SLUG "
                             "and EVOAGENT_GITHUB_SETUP_STATE"
                })
                return
            # Minting a setup state is a management action.  Exposing it to
            # anonymous callers let anyone harvest the state and bind their own
            # installation to the default tenant.
            principal = self._principal("manage")
            token = secrets.token_urlsafe(32)
            self.service.store.save_github_setup_state(
                self._setup_state_hash(token), principal.tenant_id,
                time.time() + GITHUB_SETUP_STATE_TTL_SECONDS,
            )
            location = "https://github.com/apps/%s/installations/new?%s" % (
                self.settings.github_app_slug,
                urllib.parse.urlencode({"state": token}),
            )
            self.service.store.audit(
                principal.tenant_id, principal.username, "github.install.start",
                self.settings.github_app_slug, {},
            )
            if "application/json" in self.headers.get("Accept", ""):
                self._send_json(200, {
                    "url": location, "expires_in": GITHUB_SETUP_STATE_TTL_SECONDS,
                })
                return
            self.send_response(302)
            self.send_header("Location", location)
            self.end_headers()
            return
        if path == "/github/setup":
            # GitHub redirects the browser here without a Bearer token, so a
            # one-time, tenant-bound state minted by /github/install authorizes
            # the callback.  An authenticated administrator may instead bind
            # the installation to their tenant directly.
            if not self.settings.github_setup_state:
                self._send_json(503, {
                    "error": "GitHub setup is disabled; configure EVOAGENT_GITHUB_SETUP_STATE"
                })
                return
            try:
                installation_id = int(query.get("installation_id", [""])[0])
            except ValueError:
                self._send_json(400, {"error": "missing installation_id"})
                return
            tenant_id = ""
            authorization = self.headers.get("Authorization", "")
            if authorization:
                setup_principal = self.service.auth.authenticate(authorization)
                self.service.auth.require(setup_principal, ("manage",))
                tenant_id = setup_principal.tenant_id
            state = query.get("state", [""])[0]
            if state:
                state_tenant = self.service.store.consume_github_setup_state(
                    self._setup_state_hash(state)
                )
                if state_tenant:
                    if tenant_id and tenant_id != state_tenant:
                        self._send_json(403, {
                            "error": "setup state belongs to another tenant"
                        })
                        return
                    tenant_id = tenant_id or state_tenant
                elif not tenant_id:
                    self._send_json(403, {
                        "error": "invalid or expired setup state"
                    })
                    return
            if not tenant_id:
                self._send_json(401, {
                    "error": "authentication is required to complete GitHub App setup"
                })
                return
            existing = self.service.store.installation_tenant(installation_id)
            if existing and existing != tenant_id:
                self._send_json(
                    409, {"error": "installation is already bound to another tenant"}
                )
                return
            account = query.get("account", ["github-app"])[0]
            self.service.store.save_installation(
                installation_id, account, tenant_id,
            )
            self.service.store.audit(
                tenant_id, "github-setup", "installation.bind",
                str(installation_id), {"account": account},
            )
            self.send_response(302)
            self.send_header(
                "Location",
                self.settings.public_base_url.rstrip("/") + "/#github",
            )
            self.end_headers()
            return
        principal = self._authenticate_or_send("read")
        if principal is None:
            return
        if path == "/health/detail":
            mode = resolve_mode(None, bool(self.service.llm_config))
            store_ok = bool(self.service.store.health_check())
            queue_ok = bool(self.service.queue.health_check())
            healthy = store_ok and queue_ok
            self._send_json(200 if healthy else 503, {
                "status": "ok" if healthy else "degraded",
                "dependencies": {"store": store_ok, "queue": queue_ok},
                "reviewer": self.service.reviewer.name,
                "runtime": self.service.harness.name,
                "queue": self.service.queue.backend,
                "llm_provider": self.service.llm_config.get("provider", "local"),
                "llm_model": self.service.llm_config.get("model", ""),
                "run_mode": mode.to_dict(),
                "taxonomy": public_taxonomy(),
            })
            return
        if path == "/metrics":
            self._send_text(200, metrics.prometheus(), "text/plain; version=0.0.4; charset=utf-8")
            return
        if path == "/api/dashboard":
            mode = resolve_mode(
                None, bool(self.service.llm_config)
            )
            self._send_json(200, {"stats": self.service.store.dashboard_stats(principal.tenant_id),
                                  "tasks": self.service.store.list_tasks(10, principal.tenant_id),
                                  "queue": self.service.queue.backend,
                                  "orchestrator": self.service.reviewer.name,
                                  "llm": {
                                      "enabled": bool(self.service.llm_config),
                                      "provider": self.service.llm_config.get("provider", "local"),
                                      "model": self.service.llm_config.get("model", ""),
                                  },
                                  "run_mode": mode.to_dict(),
                                  "taxonomy": public_taxonomy()})
            return
        if path == "/api/tasks":
            state = query.get("state", [""])[0].strip().upper()
            if state and state not in TASK_STATES:
                raise ValueError(
                    "state must be one of: %s" % ", ".join(sorted(TASK_STATES))
                )
            repository = query.get("repository", [""])[0].strip()
            self._send_json(200, {"tasks": self.service.store.list_tasks(
                self._query_int(query, "limit", 50), principal.tenant_id,
                state, repository,
            )})
            return
        if path == "/api/skills":
            self._send_json(200, {
                "skills": self.service.list_skills(principal.tenant_id),
                "llm": {
                    "enabled": bool(self.service.llm_config),
                    "provider": self.service.llm_config.get("provider", "local"),
                    "model": self.service.llm_config.get("model", ""),
                },
            })
            return
        if path == "/api/failures":
            if not principal.can("audit"):
                self._send_json(403, {"error": "permission denied"})
                return
            self._send_json(200, {"cases": self.service.store.list_failure_cases(
                False, 100, principal.tenant_id
            )})
            return
        if path == "/api/audit":
            if not principal.can("audit"):
                self._send_json(403, {"error": "permission denied"})
                return
            self._send_json(200, {"events": self.service.store.list_audit(
                principal.tenant_id, self._query_int(query, "limit", 100)
            )})
            return
        if path == "/api/alerts":
            self._send_json(200, {"alerts": self.service.store.list_alerts(principal.tenant_id)})
            return
        if path == GRANTS:
            if not principal.can("manage"):
                self._send_json(403, {"error": "permission denied"})
                return
            self._send_json(200, {
                "grants": self.service.store.list_repository_grants(
                    principal.tenant_id
                )
            })
            return
        if path == USERS:
            if not principal.can("manage"):
                self._send_json(403, {"error": "permission denied"})
                return
            tenant_id = query.get("tenant_id", [principal.tenant_id])[0].strip() or principal.tenant_id
            if tenant_id != principal.tenant_id and not principal.can("platform"):
                self._send_json(403, {"error": "permission denied"})
                return
            self._send_json(200, {
                "users": self.service.auth.list_tenant_users(tenant_id),
                "tenant_id": tenant_id,
            })
            return
        if path == TENANTS:
            if not principal.can("platform"):
                self._send_json(403, {"error": "platform permission denied"})
                return
            self._send_json(200, {"tenants": self.service.store.list_tenants()})
            return
        if path == TENANT_QUOTA:
            self._send_json(200, self.service.quota.status(principal.tenant_id))
            return
        if path == "/api/deployments/llm-review":
            self._send_json(200, {"deployment": self.service.store.get_deployment(
                principal.tenant_id, "llm-review"
            )})
            return
        if path == "/api/queue/dead-letters":
            if not principal.can("manage"):
                self._send_json(403, {"error": "permission denied"})
                return
            self._send_json(200, {"messages": self.service.queue.dead_letters(
                self._query_int(query, "limit", 100), principal.tenant_id
            )})
            return
        if path == "/v1/evaluation/cases":
            split = query.get("split", ["validation"])[0]
            if split == "holdout":
                self._send_json(403, {"error": "holdout cases are not exposed through the API"})
                return
            self._send_json(200, {
                "cases": self.service.store.list_evaluation_cases(split, True, 100, principal.tenant_id)
            })
            return
        if path == "/v1/evolution/runs":
            self._send_json(200, {
                "runs": self.service.store.list_evolution_runs(
                    self._query_int(query, "limit", 50), principal.tenant_id
                )
            })
            return
        if path == "/v1/evolution/status":
            status = self.service.evolution_for(principal.tenant_id).status()
            status["provider"] = self.service.llm_config.get("provider", "local")
            status["model"] = self.service.llm_config.get("model", "")
            self._send_json(200, status)
            return
        if path == "/v1/skill-evolution/status":
            if not principal.can("manage"):
                self._send_json(403, {"error": "permission denied"})
                return
            skill_name = query.get("skill_name", ["evolved-review"])[0]
            self._send_json(200, self.service.skill_evolution.status(
                skill_name, principal.tenant_id
            ))
            return
        if path == "/v1/skill-evolution/runs":
            if not principal.can("manage"):
                self._send_json(403, {"error": "permission denied"})
                return
            self._send_json(200, {"runs": self.service.store.list_skill_evolution_runs(
                self._query_int(query, "limit", 50), principal.tenant_id
            )})
            return
        match = PROMPT_VERSIONS.match(path)
        if match:
            if not principal.can("manage"):
                self._send_json(403, {"error": "permission denied"})
                return
            versions = self.service.store.list_skill_versions(
                match.group(1), principal.tenant_id,
            )
            self._send_json(200, {"versions": [{
                "version": item["version"],
                "score": item["score"],
                "active": bool(item["active"]),
                "parent_version": item.get("parent_version"),
                "created_at": item.get("created_at"),
                "prompt_sha256": hashlib.sha256(
                    str(item.get("prompt", "")).encode("utf-8")
                ).hexdigest(),
                "prompt_preview": str(item.get("prompt", ""))[:200],
            } for item in versions]})
            return
        match = SKILL_ARTIFACT_VERSIONS.match(path)
        if match:
            if not principal.can("manage"):
                self._send_json(403, {"error": "permission denied"})
                return
            self._send_json(200, {"versions": self.service.store.list_skill_artifact_versions(
                match.group(1), principal.tenant_id
            )})
            return
        report_match = REPORT.match(path)
        sarif_match = SARIF.match(path)
        task_match = TASK.match(path)
        feedback_match = FEEDBACK.match(path)
        if feedback_match:
            if not principal.can("review"):
                self._send_json(403, {"error": "permission denied"})
                return
            task = self.service.store.get(feedback_match.group(1), principal.tenant_id)
            if not task:
                self._send_json(404, {"error": "task not found"})
                return
            self._send_json(200, {"cases": self.service.store.list_task_failure_cases(
                feedback_match.group(1), principal.tenant_id
            )})
            return
        if sarif_match:
            task = self.service.store.get(sarif_match.group(1), principal.tenant_id)
            if not task or not task.get("report"):
                self._send_json(404, {"error": "task or report not found"})
                return
            self._send_json(200, to_sarif(task["report"], __version__))
            return
        if report_match:
            task = self.service.store.get(report_match.group(1), principal.tenant_id)
            if not task or not task.get("report"):
                self._send_json(404, {"error": "task or report not found"})
                return
            self._send_text(
                200,
                to_markdown(task["report"], self.settings.report_max_findings),
                "text/markdown; charset=utf-8",
            )
            return
        if task_match:
            task = self.service.store.get(task_match.group(1), principal.tenant_id)
            if not task:
                self._send_json(404, {"error": "task not found"})
                return
            self._send_json(200, task)
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path
        query = urllib.parse.parse_qs(parsed_url.query)
        try:
            body = b"" if body_optional(path) else self._read_body()
            if path == "/v1/auth/login":
                if not self.settings.auth_required:
                    self._send_json(409, {"error": "authentication is disabled"})
                    return
                payload = self._read_json(body)
                try:
                    result = self.service.auth.login(
                        str(payload.get("username", "")), str(payload.get("password", "")),
                        str(payload.get("tenant_id", "")),
                        client_key=self._client_key(),
                    )
                except PermissionError as exc:
                    self._send_json(401, {"error": str(exc)})
                    return
                self._send_json(200, result)
                return
            if path == "/v1/auth/token":
                if not self.settings.auth_required:
                    self._send_json(409, {"error": "authentication is disabled"})
                    return
                principal = self._principal("manage")
                payload = self._read_json(body)
                username = text_field(payload, "username").strip() or principal.username
                tenant_id = text_field(payload, "tenant_id").strip() or principal.tenant_id
                if username != principal.username and not principal.can("platform"):
                    raise PermissionError(
                        "only platform administrators may issue tokens for another user"
                    )
                if tenant_id != principal.tenant_id:
                    raise PermissionError("cannot issue tokens for another tenant")
                ttl = payload.get("ttl_seconds")
                if ttl is not None and (
                    not isinstance(ttl, int) or isinstance(ttl, bool)
                ):
                    raise ValueError("ttl_seconds must be an integer")
                result = self.service.auth.issue_token(username, tenant_id, ttl)
                self.service.store.audit(
                    principal.tenant_id, principal.username, "auth.token.issue",
                    username, {"tenant_id": tenant_id,
                               "expires_in": result["expires_in"]},
                )
                self._send_json(201, result)
                return
            if path == "/v1/reviews":
                principal = self._principal("review")
                payload = self._read_json(body)
                pr = payload.get("pull_request")
                if pr is not None and not isinstance(pr, int):
                    raise ValueError("pull_request must be an integer")
                args = (
                    text_field(payload, "repository"),
                    text_field(payload, "diff"),
                    pr,
                )
                enabled_agents = payload.get("enabled_agents")
                if enabled_agents is not None and (
                    not isinstance(enabled_agents, list)
                    or not all(isinstance(item, str) for item in enabled_agents)
                ):
                    raise ValueError("enabled_agents must be an array of role names")
                enabled_skills = payload.get("enabled_skills")
                if enabled_skills is not None and (
                    not isinstance(enabled_skills, list)
                    or not all(isinstance(item, str) for item in enabled_skills)
                ):
                    raise ValueError("enabled_skills must be an array of Agent Skill names")
                options = {
                    "tenant_id": principal.tenant_id,
                    "mode": text_field(payload, "mode"),
                    "repository_root": text_field(payload, "repository_root"),
                    "enabled_agents": enabled_agents,
                    "enabled_skills": enabled_skills,
                }
                if query.get("async", ["false"])[0].lower() == "true":
                    result = self.service.enqueue_review(*args, **options)
                    status = 202
                else:
                    result = self.service.create_review(*args, **options)
                    status = 201
                self.service.store.audit(
                    principal.tenant_id, principal.username, "review.create",
                    text_field(payload, "repository"), {
                        "async": query.get("async", ["false"])[0],
                        "mode": text_field(payload, "mode"),
                    },
                )
                self._send_json(status, result)
                return
            if path == "/webhooks/github":
                if self.headers.get("X-GitHub-Event", "") != "pull_request":
                    self._send_json(202, {"ignored": True, "reason": "unsupported GitHub event"})
                    return
                if not self.settings.github_webhook_secret:
                    self._send_json(503, {"error": "GitHub webhook secret is not configured"})
                    return
                if not verify_signature(self.settings.github_webhook_secret, body,
                                        self.headers.get("X-Hub-Signature-256", "")):
                    self._send_json(401, {"error": "invalid webhook signature"})
                    return
                payload = self._read_json(body)
                updated_at = (payload.get("pull_request") or {}).get("updated_at")
                if updated_at:
                    try:
                        event_time = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
                    except ValueError:
                        raise ValueError("invalid pull_request.updated_at")
                    age = abs((datetime.now(timezone.utc) - event_time).total_seconds())
                    if age > self.settings.webhook_max_age_seconds:
                        self._send_json(409, {"error": "webhook is outside the replay window"})
                        return
                delivery_id = self.headers.get("X-GitHub-Delivery", "")
                digest = hashlib.sha256(body).hexdigest()
                self._send_json(202, self.service.handle_github_pull_request(
                    payload, delivery_id, digest
                ))
                return
            match = FIX.match(path)
            if match:
                principal = self._principal("fix")
                payload = self._read_json(body)
                installation_id = payload.get("installation_id")
                if installation_id is not None and not isinstance(installation_id, int):
                    raise ValueError("installation_id must be an integer")
                result = self.service.create_fix(
                    match.group(1), installation_id, principal.tenant_id
                )
                self.service.store.audit(
                    principal.tenant_id, principal.username, "repair.create",
                    match.group(1), {"branch": result.get("branch")},
                )
                self._send_json(201, result)
                return
            match = FEEDBACK.match(path)
            if match:
                principal = self._principal("review")
                payload = self._read_json(body)
                result = self.service.record_feedback(
                    match.group(1), str(payload.get("category", "")), payload.get("finding"),
                    str(payload.get("note", "")), principal.tenant_id,
                )
                self.service.store.audit(
                    principal.tenant_id, principal.username, "feedback.record", match.group(1),
                    {"category": result["category"]},
                )
                self._send_json(201, result)
                return
            match = CANCEL.match(path)
            if match:
                principal = self._principal("review")
                ok = self.service.cancel_task(match.group(1), principal.tenant_id)
                self.service.store.audit(
                    principal.tenant_id, principal.username, "task.cancel", match.group(1)
                )
                self._send_json(202 if ok else 404, {"cancel_requested": ok})
                return
            match = RESUME.match(path)
            if match:
                principal = self._principal("review")
                result = self.service.resume_task(match.group(1), principal.tenant_id)
                self.service.store.audit(
                    principal.tenant_id, principal.username, "task.resume", match.group(1)
                )
                self._send_json(202, result)
                return
            match = COMMENT.match(path)
            if match:
                principal = self._principal("review")
                result = self.service.redeliver_comment(
                    match.group(1), principal.tenant_id
                )
                self.service.store.audit(
                    principal.tenant_id, principal.username, "comment.redeliver",
                    match.group(1), {"delivered": result["delivered"]},
                )
                self._send_json(200 if result["delivered"] else 502, result)
                return
            if path == GRANTS:
                principal = self._principal("manage")
                payload = self._read_json(body)
                repository = text_field(payload, "repository").strip()
                if not repository or len(repository) > 250:
                    raise ValueError(
                        "repository is required and must be at most 250 characters"
                    )
                auto_fix = payload.get("auto_fix", False)
                if not isinstance(auto_fix, bool):
                    raise ValueError("auto_fix must be a boolean")
                self.service.store.grant_repository(
                    principal.tenant_id, repository, auto_fix,
                )
                self.service.store.audit(
                    principal.tenant_id, principal.username, "repository.grant",
                    repository, {"auto_fix": auto_fix},
                )
                self._send_json(201, {
                    "repository": repository, "auto_fix": auto_fix,
                })
                return
            if path == TENANTS:
                principal = self._principal("platform")
                payload = self._read_json(body)
                tenant_id = text_field(payload, "tenant_id").strip()
                if not TENANT_ID.fullmatch(tenant_id):
                    raise ValueError(
                        "tenant_id must use 1-100 letters, digits, dot, underscore or hyphen"
                    )
                created = self.service.store.create_tenant(tenant_id)
                self.service.store.audit(
                    tenant_id, principal.username, "tenant.create",
                    tenant_id, {"created": created},
                )
                self._send_json(201 if created else 409, {
                    "tenant_id": tenant_id, "created": created,
                })
                return
            if path == USERS:
                principal = self._principal("manage")
                payload = self._read_json(body)
                username = text_field(payload, "username").strip()
                password = text_field(payload, "password")
                role = text_field(payload, "role").strip() or "maintainer"
                tenant_id = (
                    text_field(payload, "tenant_id").strip() or principal.tenant_id
                )
                if tenant_id != principal.tenant_id:
                    self.service.auth.require(principal, ("platform",))
                    if not self.service.store.tenant_exists(tenant_id):
                        raise ValueError("tenant does not exist")
                result = self.service.auth.create_tenant_user(
                    username, password, tenant_id, role,
                )
                self.service.store.audit(
                    tenant_id, principal.username, "user.create",
                    username, {"role": role, "created": result["created"]},
                )
                self._send_json(201, result)
                return
            match = USER_PASSWORD.match(path)
            if match:
                principal = self._principal("manage")
                payload = self._read_json(body)
                username = urllib.parse.unquote(match.group(1))
                self._check_manageable_target(username, principal)
                ok = self.service.auth.set_tenant_user_password(
                    username, text_field(payload, "password"),
                    principal.tenant_id,
                )
                self.service.store.audit(
                    principal.tenant_id, principal.username, "user.password",
                    username, {"updated": ok},
                )
                self._send_json(200 if ok else 404, {"updated": ok})
                return
            match = USER_ACTIVE.match(path)
            if match:
                principal = self._principal("manage")
                payload = self._read_json(body)
                username = urllib.parse.unquote(match.group(1))
                self._check_manageable_target(username, principal)
                if username == principal.username:
                    raise ValueError("cannot change your own active state")
                active = payload.get("active")
                if not isinstance(active, bool):
                    raise ValueError("active must be a boolean")
                ok = self.service.auth.set_tenant_user_active(
                    username, principal.tenant_id, active,
                )
                self.service.store.audit(
                    principal.tenant_id, principal.username, "user.active",
                    username, {"active": active, "updated": ok},
                )
                self._send_json(200 if ok else 404, {"updated": ok})
                return
            if path == TENANT_QUOTA:
                principal = self._principal("manage")
                payload = self._read_json(body)
                tenant_id = (
                    text_field(payload, "tenant_id").strip() or principal.tenant_id
                )
                if tenant_id != principal.tenant_id:
                    self.service.auth.require(principal, ("platform",))
                values = []
                for name in ("daily_reviews", "daily_tokens"):
                    value = payload.get(name, 0)
                    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                        raise ValueError("%s must be a non-negative integer" % name)
                    values.append(value)
                cost = payload.get("daily_cost_usd", 0.0)
                if isinstance(cost, bool) or not isinstance(cost, (int, float)) or cost < 0:
                    raise ValueError("daily_cost_usd must be a non-negative number")
                result = self.service.quota.set_limits(
                    tenant_id, values[0], values[1], float(cost),
                )
                self.service.store.audit(
                    principal.tenant_id, principal.username, "tenant.quota.update",
                    tenant_id, {"limits": result["limits"]},
                )
                self._send_json(201, result)
                return
            match = PLATFORM_ADMIN.match(path)
            if match:
                principal = self._principal("platform")
                payload = self._read_json(body)
                username = urllib.parse.unquote(match.group(1))
                active = payload.get("active")
                if not isinstance(active, bool):
                    raise ValueError("active must be a boolean")
                if username == principal.username and not active:
                    raise ValueError("cannot remove your own platform scope")
                ok = self.service.store.set_platform_admin(username, active)
                self.service.store.audit(
                    principal.tenant_id, principal.username, "user.platform_admin",
                    username, {"active": active, "updated": ok},
                )
                self._send_json(200 if ok else 404, {"updated": ok})
                return
            if path == "/v1/skills/reload":
                principal = self._principal("manage")
                skills = self.service.reload_skills()
                self.service.store.audit(
                    principal.tenant_id, principal.username, "skill.reload",
                    "skills", {"count": len(skills)},
                )
                self._send_json(200, {"skills": skills,
                                      "note": "New tasks now use the reloaded skill set."})
                return
            if path == "/v1/deployments/llm-review":
                principal = self._principal("manage")
                payload = self._read_json(body)
                result = self.service.releases.configure(
                    principal.tenant_id, "llm-review", payload
                )
                self.service.store.audit(
                    principal.tenant_id, principal.username, "deployment.configure",
                    "llm-review", payload,
                )
                self._send_json(201, result)
                return
            if path == "/v1/queue/dead-letters/replay":
                principal = self._principal("manage")
                payload = self._read_json(body)
                ok = self.service.queue.replay_dead_letter(
                    str(payload.get("message_id", "")), principal.tenant_id
                )
                self._send_json(202 if ok else 404, {"replayed": ok})
                return
            if path == "/v1/evaluation/cases":
                principal = self._principal("manage")
                payload = self._read_json(body)
                split = payload.get("split")
                result = self.service.evolution_for(principal.tenant_id).add_evaluation_case(
                    text_field(payload, "name"),
                    text_field(payload, "diff"),
                    payload.get("expected_findings", []),
                    "validation" if split is None else str(split),
                    "api",
                )
                self.service.store.audit(
                    principal.tenant_id, principal.username, "evaluation.case.add",
                    text_field(payload, "name"),
                    {"split": "validation" if split is None else str(split)},
                )
                self._send_json(201, result)
                return
            if path == "/v1/evolution/auto":
                principal = self._principal("manage")
                payload = self._read_json(body)
                result = self.service.evolution_for(principal.tenant_id).auto_propose(
                    str(payload.get("skill_name", "llm-review")), principal.tenant_id
                )
                if result["decision"] == "activated":
                    self.service.reload_skills()
                self.service.store.audit(
                    principal.tenant_id, principal.username, "prompt.evolution.auto",
                    str(payload.get("skill_name", "llm-review")),
                    {"decision": result["decision"], "run_id": result.get("run_id")},
                )
                self._send_json(201, result)
                return
            if path == "/v1/evolution/propose":
                principal = self._principal("manage")
                payload = self._read_json(body)
                result = self.service.evolution_for(principal.tenant_id).propose(
                    str(payload.get("skill_name", "")), str(payload.get("prompt", "")),
                    float(payload["regression_score"]) if "regression_score" in payload else None,
                )
                if result["decision"] == "activated":
                    self.service.reload_skills()
                self.service.store.audit(
                    principal.tenant_id, principal.username, "prompt.evolution.propose",
                    str(payload.get("skill_name", "")),
                    {"decision": result["decision"], "run_id": result.get("run_id")},
                )
                self._send_json(201, result)
                return
            if path == "/v1/skill-evolution/auto":
                principal = self._principal("manage")
                payload = self._read_json(body)
                result = self.service.skill_evolution.auto_propose(
                    str(payload.get("skill_name", "evolved-review")), principal.tenant_id
                )
                if result["decision"] == "activated":
                    self.service.reload_skills()
                self.service.store.audit(
                    principal.tenant_id, principal.username, "skill.evolution.auto",
                    str(payload.get("skill_name", "evolved-review")),
                    {"decision": result["decision"], "run_id": result.get("run_id")},
                )
                self._send_json(201, result)
                return
            if path == "/v1/skill-evolution/propose":
                principal = self._principal("manage")
                payload = self._read_json(body)
                artifact = payload.get("artifact")
                if artifact is None and "skill_md" in payload:
                    artifact = {
                        "name": str(payload.get("skill_name", "")),
                        "skill_md": payload.get("skill_md"),
                        "supporting_files": payload.get("supporting_files") or {},
                    }
                result = self.service.skill_evolution.propose(
                    str(payload.get("skill_name", "")), artifact,
                    principal.tenant_id,
                )
                if result["decision"] == "activated":
                    self.service.reload_skills()
                self.service.store.audit(
                    principal.tenant_id, principal.username, "skill.evolution.propose",
                    str(payload.get("skill_name", "")),
                    {"decision": result["decision"], "run_id": result.get("run_id")},
                )
                self._send_json(201, result)
                return
            match = SKILL_ARTIFACT_ACTIVATE.match(path)
            if match:
                principal = self._principal("manage")
                ok = self.service.skill_evolution.rollback(
                    match.group(1), int(match.group(2)), principal.tenant_id
                )
                if ok:
                    self.service.reload_skills()
                self.service.store.audit(
                    principal.tenant_id, principal.username, "skill.evolution.activate",
                    match.group(1), {"version": int(match.group(2)), "activated": ok},
                )
                self._send_json(200 if ok else 404, {"activated": ok})
                return
            match = ROLLBACK.match(path)
            if match:
                principal = self._principal("manage")
                version = int(match.group(2))
                ok = self.service.evolution_for(principal.tenant_id).rollback(
                    match.group(1), version,
                )
                if ok:
                    self.service.reload_skills()
                self.service.store.audit(
                    principal.tenant_id, principal.username, "prompt.evolution.activate",
                    match.group(1), {"version": version, "activated": ok},
                )
                self._send_json(200 if ok else 404, {"activated": ok})
                return
            match = ALERT_RESOLVE.match(path)
            if match:
                principal = self._principal("manage")
                alert_id = int(match.group(1))
                ok = self.service.store.resolve_alert(
                    principal.tenant_id, alert_id,
                )
                self.service.store.audit(
                    principal.tenant_id, principal.username, "alert.resolve",
                    str(alert_id), {"resolved": ok},
                )
                self._send_json(200 if ok else 404, {"resolved": ok})
                return
            self._send_json(404, {"error": "not found"})
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
        except AuthenticationRequired as exc:
            self._send_json(401, {"error": str(exc)})
        except PermissionError as exc:
            self._send_json(403, {"error": str(exc)})
        except ModelNotConfigured as exc:
            self._send_json(503, {"error": str(exc)})
        except QuotaExceeded as exc:
            self._send_json(429, {"error": str(exc)})
        except Exception:
            metrics.inc("http_errors_total")
            logger.exception("unhandled POST failure for %s", self.path)
            self._send_json(500, {"error": "operation failed"})

    def do_DELETE(self) -> None:
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path
        query = urllib.parse.parse_qs(parsed_url.query)
        try:
            if path == GRANTS:
                principal = self._principal("manage")
                repository = query.get("repository", [""])[0].strip()
                if not repository:
                    raise ValueError("repository is required")
                ok = self.service.store.revoke_repository(
                    principal.tenant_id, repository
                )
                self.service.store.audit(
                    principal.tenant_id, principal.username, "repository.revoke",
                    repository, {"revoked": ok},
                )
                self._send_json(200 if ok else 404, {"revoked": ok})
                return
            self._send_json(404, {"error": "not found"})
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
        except AuthenticationRequired as exc:
            self._send_json(401, {"error": str(exc)})
        except PermissionError as exc:
            self._send_json(403, {"error": str(exc)})
        except Exception:
            metrics.inc("http_errors_total")
            logger.exception("unhandled DELETE failure for %s", self.path)
            self._send_json(500, {"error": "operation failed"})


def server_class(host: str):
    """Pick the socket family that matches the configured bind address."""
    if ":" in host:
        class IPv6ThreadingHTTPServer(ThreadingHTTPServer):
            address_family = socket.AF_INET6
        return IPv6ThreadingHTTPServer
    return ThreadingHTTPServer


def install_shutdown_handlers(server: Any) -> None:
    """Turn SIGTERM into a graceful server shutdown.

    ``serve_forever`` must be stopped from another thread, so the signal
    handler delegates ``shutdown()`` instead of calling it inline.
    """
    if threading.current_thread() is not threading.main_thread():
        return

    def request_shutdown(signum, frame):
        threading.Thread(
            target=server.shutdown, name="evoagent-http-shutdown", daemon=True,
        ).start()

    try:
        signal.signal(signal.SIGTERM, request_shutdown)
    except (AttributeError, OSError, ValueError):
        # Some embedders and non-main runtimes cannot install signal handlers.
        pass


def run() -> None:
    settings = Settings.from_env()
    service = ReviewService(settings)
    handler = type("ConfiguredApiHandler", (ApiHandler,), {"service": service, "settings": settings})
    handler.timeout = 30  # bound slow client reads
    server = server_class(settings.host)((settings.host, settings.port), handler)
    server.daemon_threads = True
    install_shutdown_handlers(server)
    display_host = "[%s]" % settings.host if ":" in settings.host else settings.host
    print("EvoReview dashboard: http://%s:%d" % (display_host, settings.port))
    print("Persistence: %s | Queue: %s | Orchestrator: %s" % (
        "postgresql" if settings.database_url else "sqlite", service.queue.backend, service.reviewer.name
    ))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        service.close()
        server.server_close()
