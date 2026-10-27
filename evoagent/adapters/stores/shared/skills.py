"""Skill artifact and evolution-run persistence shared by both SQL backends."""
import hashlib
import json
from typing import Any, Dict, Optional

from ....time_utils import utc_now
from .rows import decode_json, skill_artifact_from_row


class SkillStoreMixin:
    def save_skill_artifact(
        self, skill_name: str, artifact: Dict[str, Any], score: float,
        activate: bool = False, tenant_id: str = "default",
    ) -> Dict[str, Any]:
        artifact_json = json.dumps(
            artifact, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        artifact_sha256 = hashlib.sha256(artifact_json.encode("utf-8")).hexdigest()
        with self._write() as conn:
            self._acquire_key_lock(conn, "artifact:" + skill_name)
            row = conn.execute(self._sql(
                "SELECT COALESCE(MAX(version),0) AS version FROM skill_artifact_versions "
                "WHERE tenant_id=? AND skill_name=?"
            ), (tenant_id, skill_name)).fetchone()
            version = int(row["version"]) + 1
            parent = conn.execute(self._sql(
                "SELECT version FROM skill_artifact_versions WHERE tenant_id=? "
                "AND skill_name=? AND active=TRUE ORDER BY version DESC LIMIT 1"
            ), (tenant_id, skill_name)).fetchone()
            if activate:
                conn.execute(self._sql(
                    "UPDATE skill_artifact_versions SET active=FALSE "
                    "WHERE tenant_id=? AND skill_name=?"
                ), (tenant_id, skill_name))
            created_at = utc_now()
            conn.execute(self._sql(
                "INSERT INTO skill_artifact_versions(tenant_id,skill_name,version,artifact_json,"
                "artifact_sha256,score,active,parent_version,created_at) "
                "VALUES (?,?,?,?::jsonb,?,?,?,?,?)"
            ), (
                tenant_id, skill_name, version, artifact_json, artifact_sha256,
                float(score), bool(activate),
                parent["version"] if parent else None, created_at,
            ))
        return {
            "tenant_id": tenant_id, "skill_name": skill_name, "version": version,
            "score": float(score), "active": activate,
            "parent_version": parent["version"] if parent else None,
            "artifact_sha256": artifact_sha256, "created_at": created_at,
        }

    @staticmethod
    def _decode_skill_artifact(row) -> Dict[str, Any]:
        return skill_artifact_from_row(row)

    def get_active_skill_artifact(
        self, skill_name: str, tenant_id: str = "default",
    ) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(self._sql(
                "SELECT * FROM skill_artifact_versions WHERE tenant_id=? AND skill_name=? "
                "AND active=TRUE ORDER BY version DESC LIMIT 1"
            ), (tenant_id, skill_name)).fetchone()
        return self._decode_skill_artifact(row) if row else None

    def list_active_skill_artifacts(self, tenant_id: str = "default") -> list:
        with self._connect() as conn:
            rows = conn.execute(self._sql(
                "SELECT * FROM skill_artifact_versions WHERE tenant_id=? AND active=TRUE "
                "ORDER BY skill_name"
            ), (tenant_id,)).fetchall()
        return [self._decode_skill_artifact(row) for row in rows]

    def list_skill_artifact_versions(
        self, skill_name: str, tenant_id: str = "default",
    ) -> list:
        with self._connect() as conn:
            rows = conn.execute(self._sql(
                "SELECT * FROM skill_artifact_versions WHERE tenant_id=? AND skill_name=? "
                "ORDER BY version DESC"
            ), (tenant_id, skill_name)).fetchall()
        return [self._decode_skill_artifact(row) for row in rows]

    def activate_skill_artifact(
        self, skill_name: str, version: int, tenant_id: str = "default",
    ) -> bool:
        with self._write() as conn:
            exists = conn.execute(self._sql(
                "SELECT 1 FROM skill_artifact_versions v WHERE v.tenant_id=? "
                "AND v.skill_name=? AND v.version=? AND (v.active=TRUE OR EXISTS ("
                "SELECT 1 FROM skill_evolution_runs r WHERE r.tenant_id=v.tenant_id "
                "AND r.skill_name=v.skill_name AND r.candidate_version=v.version "
                "AND r.decision='activated'))"
            ), (tenant_id, skill_name, version)).fetchone()
            if not exists:
                return False
            conn.execute(self._sql(
                "UPDATE skill_artifact_versions SET active=FALSE "
                "WHERE tenant_id=? AND skill_name=?"
            ), (tenant_id, skill_name))
            conn.execute(self._sql(
                "UPDATE skill_artifact_versions SET active=TRUE "
                "WHERE tenant_id=? AND skill_name=? AND version=?"
            ), (tenant_id, skill_name, version))
        return True

    def save_skill_evolution_run(self, run: Dict[str, Any]) -> Dict[str, Any]:
        with self._write() as conn:
            conn.execute(self._sql(
                "INSERT INTO skill_evolution_runs(id,tenant_id,skill_name,candidate_version,"
                "baseline_version,decision,candidate_score,baseline_score,metrics_json,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?::jsonb,?)"
            ), (
                run["id"], run.get("tenant_id", "default"), run["skill_name"],
                run["candidate_version"], run.get("baseline_version"),
                run["decision"], run["candidate_score"], run["baseline_score"],
                json.dumps(run["metrics"], ensure_ascii=False), run["created_at"],
            ))
        return run

    def list_skill_evolution_runs(
        self, limit: int = 50, tenant_id: Optional[str] = None,
    ) -> list:
        with self._connect() as conn:
            if tenant_id is None:
                rows = conn.execute(self._sql(
                    "SELECT * FROM skill_evolution_runs ORDER BY created_at DESC LIMIT ?"
                ), (max(1, min(limit, 200)),)).fetchall()
            else:
                rows = conn.execute(self._sql(
                    "SELECT * FROM skill_evolution_runs WHERE tenant_id=? "
                    "ORDER BY created_at DESC LIMIT ?"
                ), (tenant_id, max(1, min(limit, 200)))).fetchall()
        values = []
        for row in rows:
            value = dict(row)
            value["metrics"] = decode_json(value.pop("metrics_json"))
            if hasattr(value.get("created_at"), "isoformat"):
                value["created_at"] = value["created_at"].isoformat()
            values.append(value)
        return values
