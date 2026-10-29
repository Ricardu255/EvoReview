"""One-time data migrations shared by both SQL backends."""
import json
import logging
from typing import Dict, Tuple

from .sql import SqlStoreMixin


logger = logging.getLogger("evoagent")

# Rows written before tenancy existed are stored under the literal tenant
# ``default``.  When the deployment configures a different default tenant,
# they belong to that tenant instead of becoming invisible.
LEGACY_TENANT_TABLES = (
    "tasks",
    "installations",
    "skill_versions",
    "evaluation_cases",
    "evolution_runs",
    "skill_artifact_versions",
    "skill_evolution_runs",
    "webhook_deliveries",
    "repository_grants",
    "audit_log",
    "deployments",
    "release_observations",
    "alerts",
    "agent_memories",
)

# Tenant-scoped unique keys.  Adopting a legacy row would violate the
# constraint when the configured tenant already owns the same key, so those
# rows are left in ``default`` and retried on a later startup instead of
# aborting the whole migration.
TENANT_UNIQUE_KEYS: Dict[str, Tuple[str, ...]] = {
    "skill_versions": ("skill_name", "version"),
    "skill_artifact_versions": ("skill_name", "version"),
    "evaluation_cases": ("name",),
    "repository_grants": ("repository",),
    "deployments": ("skill_name",),
    "alerts": ("alert_key", "status"),
}

MARKER_KEY = "legacy_tenant_adopted_to"


class MigrationStoreMixin(SqlStoreMixin):
    def migrate_legacy_tenant(self, default_tenant_id: str) -> bool:
        """Adopt pre-tenancy rows into the configured default tenant once.

        The ``schema_meta`` marker makes this idempotent.  Rows that would
        collide with an existing unique key in the target tenant are skipped
        and counted in the marker; the next startup retries them, so an
        operator can resolve the collision without editing the database.
        Returns whether any data was migrated.
        """
        target = (default_tenant_id or "").strip() or "default"
        if target == "default":
            return False
        with self._write() as conn:
            marker = conn.execute(self._sql(
                "SELECT value FROM schema_meta WHERE key=?"
            ), (MARKER_KEY,)).fetchone()
            if marker:
                state = self._marker_state(marker["value"])
                if state.get("target") != target:
                    # Legacy rows already belong to a previously configured
                    # tenant; changing the default tenant must not move them
                    # again or overwrite the migration marker.
                    logger.warning(
                        "legacy tenant migration already targeted %s; "
                        "refusing to retarget it to %s",
                        state.get("target"), target,
                    )
                    return False
                if not state.get("remaining"):
                    return False
            adopted = 0
            for table in LEGACY_TENANT_TABLES:
                adopted += self._adopt_tenant_rows(conn, table, target)
            adopted += self._adopt_memberships(conn, target)
            remaining = self._remaining_legacy_rows(conn)
            if remaining:
                logger.warning(
                    "legacy tenant rows could not be adopted into %s because "
                    "the target already owns the same key: %s",
                    target,
                    ", ".join(
                        "%s=%d" % (table, count)
                        for table, count in sorted(remaining.items())
                    ),
                )
            conn.execute(self._sql(
                "INSERT INTO schema_meta(key,value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
            ), (MARKER_KEY, json.dumps(
                {"target": target, "remaining": remaining}, sort_keys=True,
            )))
        return adopted > 0

    @staticmethod
    def _marker_state(value) -> dict:
        """Parse the marker, tolerating the pre-JSON value written by 0.4.x."""
        try:
            state = json.loads(value)
        except (TypeError, ValueError):
            return {"target": value, "remaining": {}}
        return state if isinstance(state, dict) else {"target": value, "remaining": {}}

    def _adopt_tenant_rows(self, conn, table: str, target: str) -> int:
        unique = TENANT_UNIQUE_KEYS.get(table)
        if not unique:
            cursor = conn.execute(self._sql(
                "UPDATE %s SET tenant_id=? WHERE tenant_id='default'" % table
            ), (target,))
            return max(0, cursor.rowcount or 0)
        conflicts = " AND ".join(
            "existing.%s=%s.%s" % (column, table, column) for column in unique
        )
        cursor = conn.execute(self._sql(
            "UPDATE %s SET tenant_id=? WHERE tenant_id='default' "
            "AND NOT EXISTS (SELECT 1 FROM %s existing "
            "WHERE existing.tenant_id=? AND %s)"
            % (table, table, conflicts)
        ), (target, target))
        return max(0, cursor.rowcount or 0)

    def _adopt_memberships(self, conn, target: str) -> int:
        # Memberships need conflict handling: a user may already belong to the
        # configured tenant after the bootstrap administrator ran.
        cursor = conn.execute(self._sql(
            "INSERT INTO memberships(user_id,tenant_id,role) "
            "SELECT user_id,?,role FROM memberships WHERE tenant_id='default' "
            "ON CONFLICT(user_id,tenant_id) DO NOTHING"
        ), (target,))
        adopted = max(0, cursor.rowcount or 0)
        cursor = conn.execute(self._sql(
            "DELETE FROM memberships WHERE tenant_id='default'"
        ))
        return adopted + max(0, cursor.rowcount or 0)

    def _remaining_legacy_rows(self, conn) -> Dict[str, int]:
        remaining = {}
        for table in LEGACY_TENANT_TABLES:
            count = conn.execute(self._sql(
                "SELECT COUNT(*) AS n FROM %s WHERE tenant_id='default'" % table
            )).fetchone()["n"]
            if count:
                remaining[table] = int(count)
        return remaining
