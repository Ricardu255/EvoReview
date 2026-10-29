"""Tenant-scoped prompt persistence shared by the SQLite and PostgreSQL adapters."""
import json

from ....time_utils import utc_now
from .sql import SqlStoreMixin


class PromptStoreMixin(SqlStoreMixin):
    def _prompt_row(self, row):
        if row is None:
            return None
        value = dict(row)
        for source, target in (("expected_json", "expected"), ("metrics_json", "metrics")):
            if source in value:
                raw = value.pop(source)
                value[target] = raw if self._postgres_sql else json.loads(raw)
        if "active" in value:
            value["active"] = bool(value["active"])
        if hasattr(value.get("created_at"), "isoformat"):
            value["created_at"] = value["created_at"].isoformat()
        return value

    def save_evaluation_case(self, name, split, diff, expected, source="manual", active=True, tenant_id="default"):
        with self._transaction(True) as conn:
            conn.execute(self._sql(
                "INSERT INTO evaluation_cases(tenant_id,name,split,diff,expected_json,source,active,created_at) "
                "VALUES (?,?,?,?,?::jsonb,?,?,?) ON CONFLICT(tenant_id,name) DO NOTHING"
            ), (tenant_id, name, split, diff, json.dumps(expected, ensure_ascii=False), source, active, utc_now()))
            value = self._prompt_row(conn.execute(self._sql(
                "SELECT * FROM evaluation_cases WHERE tenant_id=? AND name=?"
            ), (tenant_id, name)).fetchone())
            if value["split"] != split or value["diff"] != diff or value["expected"] != expected:
                raise ValueError("evaluation case names are immutable; use a new name for revised content")
            return value

    def list_evaluation_cases(self, split=None, active_only=True, limit=100, tenant_id="default"):
        clauses, params = ["tenant_id=?"], [tenant_id]
        if split:
            clauses.append("split=?")
            params.append(split)
        if active_only:
            clauses.append("active=?")
            params.append(True)
        params.append(max(1, min(int(limit), 500)))
        with self._transaction() as conn:
            rows = conn.execute(self._sql(
                "SELECT * FROM evaluation_cases WHERE " + " AND ".join(clauses) + " ORDER BY id LIMIT ?"
            ), params).fetchall()
        return [self._prompt_row(row) for row in rows]

    def save_evolution_run(self, run):
        with self._transaction(True) as conn:
            conn.execute(self._sql(
                "INSERT INTO evolution_runs(id,tenant_id,skill_name,candidate_version,baseline_version,decision,"
                "candidate_score,baseline_score,metrics_json,created_at) VALUES (?,?,?,?,?,?,?,?,?::jsonb,?)"
            ), (run["id"], run.get("tenant_id", "default"), run["skill_name"], run["candidate_version"],
                run.get("baseline_version"), run["decision"], run["candidate_score"], run["baseline_score"],
                json.dumps(run["metrics"], ensure_ascii=False), run["created_at"]))
        return run

    def list_evolution_runs(self, limit=50, tenant_id="default"):
        with self._transaction() as conn:
            rows = conn.execute(self._sql(
                "SELECT * FROM evolution_runs WHERE tenant_id=? ORDER BY created_at DESC LIMIT ?"
            ), (tenant_id, max(1, min(int(limit), 200)))).fetchall()
        return [self._prompt_row(row) for row in rows]

    def update_evolution_run(self, run_id, decision, metrics, tenant_id="default"):
        with self._transaction(True) as conn:
            return conn.execute(self._sql(
                "UPDATE evolution_runs SET decision=?,metrics_json=?::jsonb WHERE id=? AND tenant_id=?"
            ), (decision, json.dumps(metrics, ensure_ascii=False), run_id, tenant_id)).rowcount == 1

    def save_skill_version(self, skill_name, prompt, score, activate=False, tenant_id="default"):
        with self._transaction(True, json.dumps([tenant_id, skill_name])) as conn:
            row = conn.execute(self._sql(
                "SELECT COALESCE(MAX(version),0) AS version FROM skill_versions WHERE tenant_id=? AND skill_name=?"
            ), (tenant_id, skill_name)).fetchone()
            version = int(row["version"]) + 1
            parent = conn.execute(self._sql(
                "SELECT version FROM skill_versions WHERE tenant_id=? AND skill_name=? AND active=?"
            ), (tenant_id, skill_name, True)).fetchone()
            if activate:
                conn.execute(self._sql(
                    "UPDATE skill_versions SET active=? WHERE tenant_id=? AND skill_name=?"
                ), (False, tenant_id, skill_name))
            conn.execute(self._sql(
                "INSERT INTO skill_versions(tenant_id,skill_name,version,prompt,score,active,parent_version,created_at) "
                "VALUES (?,?,?,?,?,?,?,?)"
            ), (tenant_id, skill_name, version, prompt, score, activate, parent["version"] if parent else None, utc_now()))
        return {"tenant_id": tenant_id, "skill_name": skill_name, "version": version, "score": score, "active": activate}

    def get_active_skill_version(self, skill_name, tenant_id="default"):
        with self._transaction() as conn:
            return self._prompt_row(conn.execute(self._sql(
                "SELECT * FROM skill_versions WHERE tenant_id=? AND skill_name=? AND active=? ORDER BY version DESC LIMIT 1"
            ), (tenant_id, skill_name, True)).fetchone())

    def get_skill_version(self, skill_name, version, tenant_id="default"):
        with self._transaction() as conn:
            return self._prompt_row(conn.execute(self._sql(
                "SELECT * FROM skill_versions WHERE tenant_id=? AND skill_name=? AND version=?"
            ), (tenant_id, skill_name, version)).fetchone())

    def list_skill_versions(self, skill_name, tenant_id="default"):
        with self._transaction() as conn:
            rows = conn.execute(self._sql(
                "SELECT * FROM skill_versions WHERE tenant_id=? AND skill_name=? ORDER BY version DESC"
            ), (tenant_id, skill_name)).fetchall()
        return [self._prompt_row(row) for row in rows]

    def activate_skill_version(self, skill_name, version, tenant_id="default"):
        with self._transaction(True, json.dumps([tenant_id, skill_name])) as conn:
            if not conn.execute(self._sql(
                "SELECT 1 FROM skill_versions WHERE tenant_id=? AND skill_name=? AND version=?"
            ), (tenant_id, skill_name, version)).fetchone():
                return False
            conn.execute(self._sql(
                "UPDATE skill_versions SET active=? WHERE tenant_id=? AND skill_name=?"
            ), (False, tenant_id, skill_name))
            conn.execute(self._sql(
                "UPDATE skill_versions SET active=? WHERE tenant_id=? AND skill_name=? AND version=?"
            ), (True, tenant_id, skill_name, version))
        return True
