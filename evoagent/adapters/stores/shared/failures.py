"""Failure-case persistence shared by both SQL backends."""
import json
from typing import Any, Dict, Optional

from ....time_utils import utc_now
from .rows import decode_json


class FailureStoreMixin:
    def record_failure_case(
        self, task_id: str, category: str, payload: Dict[str, Any],
    ) -> None:
        with self._write() as conn:
            conn.execute(self._sql(
                "INSERT INTO failure_cases(task_id,category,payload_json,created_at) "
                "VALUES (?,?,?::jsonb,?)"
            ), (task_id, category, json.dumps(payload, ensure_ascii=False), utc_now()))

    def _failure_case_rows(self, rows):
        values = []
        for row in rows:
            item = dict(row)
            item["payload"] = decode_json(item.pop("payload_json"))
            if hasattr(item.get("created_at"), "isoformat"):
                item["created_at"] = item["created_at"].isoformat()
            values.append(item)
        return values

    def list_failure_cases(
        self, unresolved_only: bool = False, limit: int = 100,
        tenant_id: Optional[str] = None,
    ) -> list:
        joins = " f"
        clauses, params = [], []
        if tenant_id is not None:
            joins += " JOIN tasks t ON t.id=f.task_id"
            clauses.append("t.tenant_id=?")
            params.append(tenant_id)
        if unresolved_only:
            clauses.append("f.resolved=FALSE")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(max(1, min(limit, 500)))
        with self._connect() as conn:
            rows = conn.execute(self._sql(
                "SELECT f.* FROM failure_cases" + joins + where
                + " ORDER BY f.id DESC LIMIT ?"
            ), params).fetchall()
        return self._failure_case_rows(rows)

    def list_task_failure_cases(
        self, task_id: str, tenant_id: Optional[str] = None,
    ) -> list:
        joins = " f"
        clauses, params = ["f.task_id=?"], [task_id]
        if tenant_id is not None:
            joins += " JOIN tasks t ON t.id=f.task_id"
            clauses.append("t.tenant_id=?")
            params.append(tenant_id)
        with self._connect() as conn:
            rows = conn.execute(self._sql(
                "SELECT f.* FROM failure_cases" + joins
                + " WHERE " + " AND ".join(clauses)
                + " ORDER BY f.id DESC"
            ), params).fetchall()
        return self._failure_case_rows(rows)

    def resolve_failure_cases(self, case_ids: list) -> None:
        ids = [int(value) for value in case_ids]
        if not ids:
            return
        with self._write() as conn:
            if self._postgres_sql:
                conn.execute(
                    "UPDATE failure_cases SET resolved=TRUE WHERE id=ANY(%s)", (ids,),
                )
            else:
                placeholders = ",".join("?" for _ in ids)
                conn.execute(
                    "UPDATE failure_cases SET resolved=TRUE WHERE id IN (%s)"
                    % placeholders, ids,
                )
