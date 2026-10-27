"""Sqlite observability persistence mixin."""
import json
from typing import Any, Dict, Optional

from ....time_utils import utc_now


class SqliteObservabilityStoreMixin:
    def save_deployment(self, tenant_id: str, skill_name: str, config: Dict[str, Any]) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO deployments(tenant_id,skill_name,stable_version,candidate_version,"
                "canary_percent,shadow_percent,max_error_rate,min_samples,status,samples,errors,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,0,0,?) ON CONFLICT(tenant_id,skill_name) DO UPDATE SET "
                "stable_version=excluded.stable_version,candidate_version=excluded.candidate_version,"
                "canary_percent=excluded.canary_percent,shadow_percent=excluded.shadow_percent,"
                "max_error_rate=excluded.max_error_rate,min_samples=excluded.min_samples,"
                "status=excluded.status,samples=0,errors=0,updated_at=excluded.updated_at",
                (tenant_id, skill_name, config.get("stable_version"), config.get("candidate_version"),
                 int(config.get("canary_percent", 0)), int(config.get("shadow_percent", 0)),
                 float(config.get("max_error_rate", .1)), int(config.get("min_samples", 20)),
                 config.get("status", "running"), utc_now()),
            )
            conn.execute(
                "UPDATE deployments SET max_disagreement_rate=?,auto_promote=?,"
                "shadow_samples=0,disagreements=0 WHERE tenant_id=? AND skill_name=?",
                (float(config.get("max_disagreement_rate", .2)),
                 int(bool(config.get("auto_promote", False))), tenant_id, skill_name),
            )

    def get_deployment(self, tenant_id: str, skill_name: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM deployments WHERE tenant_id=? AND skill_name=?",
                (tenant_id, skill_name),
            ).fetchone()
        return dict(row) if row else None

    def record_deployment_result(
        self, tenant_id: str, skill_name: str, failed: bool,
    ) -> Optional[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE deployments SET samples=samples+1,errors=errors+?,updated_at=? "
                "WHERE tenant_id=? AND skill_name=?",
                (int(failed), utc_now(), tenant_id, skill_name),
            )
            row = conn.execute(
                "SELECT * FROM deployments WHERE tenant_id=? AND skill_name=?",
                (tenant_id, skill_name),
            ).fetchone()
            if not row:
                return None
            value = dict(row)
            if (
                value["status"] == "running"
                and value["samples"] >= value["min_samples"]
                and value["errors"] / value["samples"] > value["max_error_rate"]
            ):
                conn.execute(
                    "UPDATE deployments SET status='rolled_back',canary_percent=0,"
                    "shadow_percent=0,updated_at=? WHERE tenant_id=? AND skill_name=?",
                    (utc_now(), tenant_id, skill_name),
                )
                value["status"] = "rolled_back"
        return value

    def record_shadow_observation(
        self, tenant_id: str, skill_name: str, task_id: str, lane: str,
        primary: Dict[str, Any], candidate: Optional[Dict[str, Any]],
        disagreement: float, candidate_failed: bool = False,
    ) -> Optional[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT id FROM release_observations WHERE tenant_id=? AND skill_name=? AND task_id=? AND lane=?",
                (tenant_id, skill_name, task_id, lane),
            ).fetchone()
            if existing:
                row = conn.execute(
                    "SELECT * FROM deployments WHERE tenant_id=? AND skill_name=?", (tenant_id, skill_name),
                ).fetchone()
                return dict(row) if row else None
            conn.execute(
                "INSERT INTO release_observations(tenant_id,skill_name,task_id,lane,"
                "primary_json,candidate_json,disagreement,candidate_failed,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (tenant_id, skill_name, task_id, lane,
                 json.dumps(primary, ensure_ascii=False),
                 json.dumps(candidate, ensure_ascii=False) if candidate is not None else None,
                 float(disagreement), int(candidate_failed), utc_now()),
            )
            conn.execute(
                "UPDATE deployments SET shadow_samples=shadow_samples+1,"
                "disagreements=disagreements+?,updated_at=? "
                "WHERE tenant_id=? AND skill_name=?",
                (int(disagreement > 0), utc_now(), tenant_id, skill_name),
            )
            row = conn.execute(
                "SELECT * FROM deployments WHERE tenant_id=? AND skill_name=?",
                (tenant_id, skill_name),
            ).fetchone()
            if not row:
                return None
            value = dict(row)
            disagreement_rate = (
                value["disagreements"] / value["shadow_samples"]
                if value["shadow_samples"] else 0.0
            )
            error_rate = value["errors"] / value["samples"] if value["samples"] else 0.0
            if (
                value["status"] == "running" and value["auto_promote"]
                and value["shadow_samples"] >= value["min_samples"]
                and disagreement_rate <= value["max_disagreement_rate"]
                and error_rate <= value["max_error_rate"]
                and not candidate_failed
            ):
                conn.execute(
                    "UPDATE deployments SET status='promoted',stable_version=candidate_version,"
                    "canary_percent=0,shadow_percent=0,updated_at=? "
                    "WHERE tenant_id=? AND skill_name=?",
                    (utc_now(), tenant_id, skill_name),
                )
                value["status"] = "promoted"
        return value

    def list_release_observations(
        self, tenant_id: str, skill_name: str, limit: int = 100,
    ) -> list:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM release_observations WHERE tenant_id=? AND skill_name=? "
                "ORDER BY id DESC LIMIT ?",
                (tenant_id, skill_name, max(1, min(limit, 500))),
            ).fetchall()
        values = []
        for row in rows:
            item = dict(row)
            item["primary"] = json.loads(item.pop("primary_json"))
            raw = item.pop("candidate_json")
            item["candidate"] = json.loads(raw) if raw else None
            values.append(item)
        return values

    def create_alert(
        self, tenant_id: str, alert_key: str, severity: str, message: str,
    ) -> None:
        now = utc_now()
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO alerts"
                "(tenant_id,alert_key,severity,message,status,created_at,updated_at) "
                "VALUES (?,?,?,?, 'open',?,?)",
                (tenant_id, alert_key, severity, message[:1000], now, now),
            )

    def list_alerts(self, tenant_id: str, limit: int = 100) -> list:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM alerts WHERE tenant_id=? ORDER BY id DESC LIMIT ?",
                (tenant_id, max(1, min(limit, 500))),
            ).fetchall()
        return [dict(row) for row in rows]

    def dashboard_stats(self, tenant_id: Optional[str] = None) -> Dict[str, Any]:
        with self._connect() as conn:
            clause = " WHERE tenant_id=?" if tenant_id is not None else ""
            params = (tenant_id,) if tenant_id is not None else ()
            total = conn.execute("SELECT COUNT(*) AS n FROM tasks" + clause, params).fetchone()["n"]
            state_prefix = clause + (" AND " if clause else " WHERE ")
            success = conn.execute(
                "SELECT COUNT(*) AS n FROM tasks" + state_prefix + "state='SUCCESS'", params
            ).fetchone()["n"]
            failed = conn.execute(
                "SELECT COUNT(*) AS n FROM tasks" + state_prefix + "state='FAILED'", params
            ).fetchone()["n"]
            if tenant_id is None:
                failures = conn.execute(
                    "SELECT COUNT(*) AS n FROM failure_cases WHERE resolved=0"
                ).fetchone()["n"]
            else:
                failures = conn.execute(
                    "SELECT COUNT(*) AS n FROM failure_cases f JOIN tasks t ON t.id=f.task_id "
                    "WHERE f.resolved=0 AND t.tenant_id=?", (tenant_id,)
                ).fetchone()["n"]
            active_skills = conn.execute(
                "SELECT COUNT(*) AS n FROM skill_versions WHERE active = 1"
            ).fetchone()["n"]
            if tenant_id is None:
                active_skills += conn.execute(
                    "SELECT COUNT(*) AS n FROM skill_artifact_versions WHERE active=1"
                ).fetchone()["n"]
            else:
                active_skills += conn.execute(
                    "SELECT COUNT(*) AS n FROM skill_artifact_versions "
                    "WHERE tenant_id=? AND active=1", (tenant_id,)
                ).fetchone()["n"]
        return {
            "tasks_total": total, "tasks_success": success, "tasks_failed": failed,
            "success_rate": round(success / total, 4) if total else 0.0,
            "unresolved_failure_cases": failures, "active_skill_versions": active_skills,
        }
