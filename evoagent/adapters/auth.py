"""Authentication, signed sessions and tenant-aware role checks."""
import base64
import hashlib
import hmac
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Dict, Iterable, Optional


ROLE_PERMISSIONS = {
    "admin": {"read", "review", "fix", "manage", "audit"},
    "maintainer": {"read", "review", "fix"},
    "auditor": {"read", "audit"},
}

# Bound throttle state even when an attacker rotates source addresses and
# usernames; stale entries are removed first and only then is the oldest key
# evicted instead of refusing to track new attempts.
MAX_LOGIN_FAILURE_KEYS = 20_000
LOGIN_PRUNE_INTERVAL_SECONDS = 60.0
CLIENT_FAILURE_MULTIPLIER = 5


def hash_password(password: str, salt: Optional[bytes] = None) -> str:
    if len(password) < 10:
        raise ValueError("password must contain at least 10 characters")
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 310_000)
    return "pbkdf2_sha256$310000$%s$%s" % (
        base64.urlsafe_b64encode(salt).decode("ascii").rstrip("="),
        base64.urlsafe_b64encode(digest).decode("ascii").rstrip("="),
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds, salt_text, digest_text = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_text + "=" * (-len(salt_text) % 4))
        expected = base64.urlsafe_b64decode(digest_text + "=" * (-len(digest_text) % 4))
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(rounds))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


@dataclass(frozen=True)
class Principal:
    user_id: str
    username: str
    tenant_id: str
    role: str

    def can(self, permission: str) -> bool:
        return permission in ROLE_PERMISSIONS.get(self.role, set())


class AuthManager:
    def __init__(
        self, store, secret: str, ttl_seconds: int = 3600,
        bootstrap_username: str = "", bootstrap_password: str = "",
        default_tenant_id: str = "default",
        login_max_failures: int = 10, login_window_seconds: int = 300,
    ):
        self.store = store
        self.secret = secret.encode("utf-8")
        self.ttl_seconds = ttl_seconds
        self.default_tenant_id = default_tenant_id
        self.login_max_failures = max(1, int(login_max_failures))
        self.login_window_seconds = max(1, int(login_window_seconds))
        # Equalize the work done for unknown users so login timing does not
        # disclose whether a username exists.
        self._dummy_hash = hash_password(uuid.uuid4().hex)
        self._login_failures = {}
        self._login_lock = threading.Lock()
        self._last_login_prune = 0.0
        if bootstrap_username and bootstrap_password:
            self.store.create_user(
                str(uuid.uuid5(uuid.NAMESPACE_URL, "evoagent:" + bootstrap_username)),
                bootstrap_username, hash_password(bootstrap_password),
                default_tenant_id, "admin",
            )

    def _login_keys(self, username: str, client_key: str):
        # One key stops password guessing against a single account from one
        # address; the higher client-wide limit also stops username spraying.
        return (
            ((client_key, "user", username), self.login_max_failures),
            (
                (client_key, "client"),
                self.login_max_failures * CLIENT_FAILURE_MULTIPLIER,
            ),
        )

    def _prune_login_failures_locked(self, now: float) -> None:
        if (
            len(self._login_failures) < MAX_LOGIN_FAILURE_KEYS
            and now - self._last_login_prune < LOGIN_PRUNE_INTERVAL_SECONDS
        ):
            return
        self._last_login_prune = now
        cutoff = now - self.login_window_seconds
        for stale in [
            key for key, attempts in self._login_failures.items()
            if not attempts or attempts[-1] < cutoff
        ]:
            self._login_failures.pop(stale, None)

    def _login_allowed(self, keys) -> bool:
        now = time.monotonic()
        with self._login_lock:
            self._prune_login_failures_locked(now)
            for key, limit in keys:
                recent = [
                    item for item in self._login_failures.get(key, [])
                    if now - item < self.login_window_seconds
                ]
                if recent:
                    self._login_failures[key] = recent
                else:
                    self._login_failures.pop(key, None)
                if len(recent) >= limit:
                    return False
            return True

    def _record_login_failures(self, keys) -> None:
        now = time.monotonic()
        with self._login_lock:
            self._prune_login_failures_locked(now)
            for key, limit in keys:
                if key not in self._login_failures:
                    while len(self._login_failures) >= MAX_LOGIN_FAILURE_KEYS:
                        oldest = next(iter(self._login_failures), None)
                        if oldest is None:
                            break
                        self._login_failures.pop(oldest, None)
                attempts = self._login_failures.setdefault(key, [])
                attempts.append(now)
                if len(attempts) > limit:
                    del attempts[:-limit]

    def _clear_login_failures(self, key) -> None:
        with self._login_lock:
            self._login_failures.pop(key, None)

    def login(
        self, username: str, password: str, tenant_id: str = "",
        client_key: str = "",
    ) -> Dict[str, object]:
        username = str(username)[:255]
        client_key = str(client_key)[:128]
        keys = self._login_keys(username, client_key)
        if not self._login_allowed(keys):
            raise PermissionError("too many failed login attempts; try again later")
        password = str(password)
        if len(password) > 1024:
            self._record_login_failures(keys)
            raise PermissionError("invalid username or password")
        user = self.store.get_user(username)
        encoded = user["password_hash"] if user else self._dummy_hash
        password_ok = verify_password(password, encoded)
        if not user or not user["active"] or not password_ok:
            self._record_login_failures(keys)
            raise PermissionError("invalid username or password")
        self._clear_login_failures(keys[0][0])
        memberships = {item["tenant_id"]: item["role"] for item in user["memberships"]}
        selected = tenant_id or (next(iter(memberships)) if len(memberships) == 1 else "")
        if not selected or selected not in memberships:
            raise PermissionError("user is not a member of the requested tenant")
        now = int(time.time())
        payload = {
            "sub": user["id"], "username": user["username"], "tenant": selected,
            "role": memberships[selected], "iat": now, "exp": now + self.ttl_seconds,
        }
        token = self._encode(payload)
        return {"access_token": token, "token_type": "Bearer", "expires_in": self.ttl_seconds,
                "tenant_id": selected, "role": memberships[selected]}

    def authenticate(self, authorization: str) -> Principal:
        if not authorization.startswith("Bearer "):
            raise PermissionError("Bearer token is required")
        payload = self._decode(authorization[7:].strip())
        # A signed token is not enough: users can be disabled or removed from a
        # tenant, and roles can change, before the token expires.  Re-check the
        # current account state so revocation does not wait for the TTL.
        user = self.store.get_user(str(payload.get("username", "")))
        if not user or not user.get("active"):
            raise PermissionError("invalid token")
        memberships = {
            str(item["tenant_id"]): str(item["role"])
            for item in user.get("memberships", [])
        }
        tenant = str(payload.get("tenant", ""))
        if memberships.get(tenant) != str(payload.get("role", "")):
            raise PermissionError("invalid token")
        return Principal(
            str(payload["sub"]), str(payload["username"]),
            tenant, str(payload["role"]),
        )

    @staticmethod
    def require(principal: Principal, permissions: Iterable[str]) -> None:
        missing = [permission for permission in permissions if not principal.can(permission)]
        if missing:
            raise PermissionError("permission denied")

    def _encode(self, payload: Dict[str, object]) -> str:
        header = _b64(b'{"alg":"HS256","typ":"JWT"}')
        body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
        signing_input = (header + "." + body).encode("ascii")
        signature = _b64(hmac.new(self.secret, signing_input, hashlib.sha256).digest())
        return header + "." + body + "." + signature

    def _decode(self, token: str) -> Dict[str, object]:
        try:
            header, body, supplied = token.split(".", 2)
            signing_input = (header + "." + body).encode("ascii")
            expected = _b64(hmac.new(self.secret, signing_input, hashlib.sha256).digest())
            if not hmac.compare_digest(expected, supplied):
                raise PermissionError("invalid token")
            payload = json.loads(_unb64(body).decode("utf-8"))
            if int(payload["exp"]) < int(time.time()):
                raise PermissionError("token has expired")
            if payload.get("role") not in ROLE_PERMISSIONS:
                raise PermissionError("invalid role")
            return payload
        except PermissionError:
            raise
        except Exception as exc:
            raise PermissionError("invalid token") from exc
