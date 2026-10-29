"""Users, repository grants and audit rows shared by both SQL backends."""
import json
from typing import Any, Dict, Optional

from ....time_utils import utc_now
from .rows import decode_json


class AuthStoreMixin:
    def create_user(
        self, user_id: str, username: str, password_hash: str,
        tenant_id: str, role: str,
    ) -> None:
        with self._write() as conn:
            conn.execute(self._sql(
                "INSERT INTO users(id,username,password_hash,created_at) VALUES (?,?,?,?) "
                "ON CONFLICT(username) DO NOTHING"
            ), (user_id, username, password_hash, utc_now()))
            row = conn.execute(
                self._sql("SELECT id FROM users WHERE username=?"), (username,),
            ).fetchone()
            conn.execute(self._sql(
                "INSERT INTO memberships(user_id,tenant_id,role) VALUES (?,?,?) "
                "ON CONFLICT(user_id,tenant_id) DO UPDATE SET role=excluded.role"
            ), (row["id"], tenant_id, role))

    def get_user(self, username: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(self._sql(
                "SELECT id,username,password_hash,active FROM users WHERE username=?"
            ), (username,)).fetchone()
            if not row:
                return None
            memberships = conn.execute(self._sql(
                "SELECT tenant_id,role FROM memberships WHERE user_id=?"
            ), (row["id"],)).fetchall()
        value = dict(row)
        value["memberships"] = [dict(item) for item in memberships]
        return value

    def grant_repository(
        self, tenant_id: str, repository: str, auto_fix: bool = False,
    ) -> None:
        with self._write() as conn:
            conn.execute(self._sql(
                "INSERT INTO repository_grants(tenant_id,repository,auto_fix) VALUES (?,?,?) "
                "ON CONFLICT(tenant_id,repository) DO UPDATE SET auto_fix=excluded.auto_fix"
            ), (tenant_id, repository, bool(auto_fix)))

    def has_repository_grants(self, tenant_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(self._sql(
                "SELECT COUNT(*) AS n FROM repository_grants WHERE tenant_id=?"
            ), (tenant_id,)).fetchone()
        return int(row["n"]) > 0

    def repository_allowed(
        self, tenant_id: str, repository: str, require_auto_fix: bool = False,
    ) -> bool:
        with self._connect() as conn:
            total = conn.execute(self._sql(
                "SELECT COUNT(*) AS n FROM repository_grants WHERE tenant_id=?"
            ), (tenant_id,)).fetchone()["n"]
            row = conn.execute(self._sql(
                "SELECT auto_fix FROM repository_grants WHERE tenant_id=? AND repository=?"
            ), (tenant_id, repository)).fetchone()
        if total == 0:
            # Reviews remain open for backward compatibility in single-tenant
            # installs, but automatic repair must always be granted explicitly.
            return not require_auto_fix
        return bool(row and (not require_auto_fix or row["auto_fix"]))

    def audit(
        self, tenant_id: str, actor: str, action: str, resource: str,
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        with self._write() as conn:
            conn.execute(self._sql(
                "INSERT INTO audit_log(tenant_id,actor,action,resource,detail_json,created_at) "
                "VALUES (?,?,?,?,?::jsonb,?)"
            ), (
                tenant_id, actor, action, resource,
                json.dumps(detail or {}, ensure_ascii=False), utc_now(),
            ))

    def list_audit(self, tenant_id: str, limit: int = 100) -> list:
        with self._connect() as conn:
            rows = conn.execute(self._sql(
                "SELECT actor,action,resource,detail_json,created_at FROM audit_log "
                "WHERE tenant_id=? ORDER BY id DESC LIMIT ?"
            ), (tenant_id, max(1, min(limit, 500)))).fetchall()
        values = []
        for row in rows:
            item = dict(row)
            item["detail"] = decode_json(item.pop("detail_json"))
            if hasattr(item.get("created_at"), "isoformat"):
                item["created_at"] = item["created_at"].isoformat()
            values.append(item)
        return values
