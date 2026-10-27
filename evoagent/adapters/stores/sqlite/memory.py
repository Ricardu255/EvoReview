"""Sqlite memory persistence mixin."""
import hashlib
import json
import sqlite3
from typing import Any, Dict, Optional

from ....models import ReviewReport, TaskState, TraceEvent
from ....time_utils import utc_now
from ..shared.rows import memory_from_row


class SqliteMemoryStoreMixin:
    def save_agent_memory(self, memory: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO agent_memories(id,tenant_id,repository,task_id,agent,scope,kind,"
                "content,keywords_json,metadata_json,importance,created_at,expires_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "importance=MAX(agent_memories.importance,excluded.importance),"
                "expires_at=excluded.expires_at",
                (
                    memory["id"], memory["tenant_id"], memory["repository"],
                    memory.get("task_id", ""), memory.get("agent", ""), memory["scope"],
                    memory["kind"], memory["content"],
                    json.dumps(memory.get("keywords", []), ensure_ascii=False),
                    json.dumps(memory.get("metadata", {}), ensure_ascii=False),
                    float(memory.get("importance", 0.5)), memory["created_at"],
                    memory.get("expires_at"),
                ),
            )
            row = conn.execute(
                "SELECT * FROM agent_memories WHERE id=?", (memory["id"],)
            ).fetchone()
        return self._memory_from_row(row)

    def list_agent_memories(
        self, tenant_id: str, repository: str, scopes: tuple,
        limit: int = 100,
    ) -> list:
        placeholders = ",".join("?" for _ in scopes)
        params = [tenant_id, repository, *scopes, utc_now(), max(1, min(limit, 500))]
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM agent_memories WHERE tenant_id=? AND repository=? "
                "AND scope IN (%s) AND (expires_at IS NULL OR expires_at>?) "
                "ORDER BY importance DESC,created_at DESC LIMIT ?" % placeholders,
                params,
            ).fetchall()
        return [self._memory_from_row(row) for row in rows]

    def delete_agent_memories(self, task_id: str = "", scope: str = "") -> int:
        clauses = []
        params = []
        if task_id:
            clauses.append("task_id=?")
            params.append(task_id)
        if scope:
            clauses.append("scope=?")
            params.append(scope)
        if not clauses:
            raise ValueError("memory deletion requires task_id or scope")
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM agent_memories WHERE " + " AND ".join(clauses), params
            )
            return cursor.rowcount

    def purge_expired_agent_memories(self) -> int:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM agent_memories WHERE expires_at IS NOT NULL AND expires_at<=?",
                (utc_now(),),
            )
            return cursor.rowcount

    @staticmethod
    def _memory_from_row(row) -> Dict[str, Any]:
        return memory_from_row(row)
