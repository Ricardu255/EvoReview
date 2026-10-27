"""Sqlite schema initialization and migrations."""
import hashlib
import json
import sqlite3
from typing import Any, Dict, Optional

from ....models import ReviewReport, TaskState, TraceEvent
from ....time_utils import utc_now


class SqliteSchemaMixin:
    def _init(self) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._migrate_prompt_tables(conn)
            conn.execute(
                """CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    repository TEXT NOT NULL,
                    pull_request INTEGER,
                    input_json TEXT NOT NULL,
                    report_json TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS failure_cases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    category TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    resolved INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS skill_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL DEFAULT 'default',
                    skill_name TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    prompt TEXT NOT NULL,
                    score REAL NOT NULL,
                    active INTEGER NOT NULL DEFAULT 0,
                    parent_version INTEGER,
                    created_at TEXT NOT NULL,
                    UNIQUE(tenant_id, skill_name, version)
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS installations (
                    installation_id INTEGER PRIMARY KEY,
                    account_login TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS trace_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    step INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES tasks(id)
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS evaluation_cases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL DEFAULT 'default',
                    name TEXT NOT NULL,
                    split TEXT NOT NULL,
                    diff TEXT NOT NULL,
                    expected_json TEXT NOT NULL,
                    source TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    UNIQUE(tenant_id, name)
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS evolution_runs (
                    id TEXT PRIMARY KEY,
                    skill_name TEXT NOT NULL,
                    candidate_version INTEGER NOT NULL,
                    baseline_version INTEGER,
                    decision TEXT NOT NULL,
                    candidate_score REAL NOT NULL,
                    baseline_score REAL NOT NULL,
                    metrics_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS skill_artifact_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL DEFAULT 'default',
                    skill_name TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    artifact_json TEXT NOT NULL,
                    artifact_sha256 TEXT NOT NULL,
                    score REAL NOT NULL,
                    active INTEGER NOT NULL DEFAULT 0,
                    parent_version INTEGER,
                    created_at TEXT NOT NULL,
                    UNIQUE(tenant_id, skill_name, version)
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS skill_evolution_runs (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL DEFAULT 'default',
                    skill_name TEXT NOT NULL,
                    candidate_version INTEGER NOT NULL,
                    baseline_version INTEGER,
                    decision TEXT NOT NULL,
                    candidate_score REAL NOT NULL,
                    baseline_score REAL NOT NULL,
                    metrics_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )"""
            )
            self._ensure_column(
                conn, "skill_artifact_versions", "tenant_id", "TEXT NOT NULL DEFAULT 'default'"
            )
            self._ensure_column(conn, "evolution_runs", "tenant_id", "TEXT NOT NULL DEFAULT 'default'")
            self._ensure_column(
                conn, "skill_evolution_runs", "tenant_id", "TEXT NOT NULL DEFAULT 'default'"
            )
            self._ensure_column(conn, "tasks", "tenant_id", "TEXT NOT NULL DEFAULT 'default'")
            self._ensure_column(conn, "tasks", "cancel_requested", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "installations", "tenant_id", "TEXT NOT NULL DEFAULT 'default'")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS checkpoints (
                    task_id TEXT NOT NULL,
                    node TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 1,
                    state_json TEXT NOT NULL,
                    error TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(task_id, node),
                    FOREIGN KEY(task_id) REFERENCES tasks(id)
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS task_payloads (
                    task_id TEXT PRIMARY KEY,
                    diff TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES tasks(id)
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS agent_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    recipient TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    correlation_id TEXT NOT NULL,
                    content_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES tasks(id)
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS webhook_deliveries (
                    delivery_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    task_id TEXT,
                    received_at TEXT NOT NULL
                )"""
            )
            for column, declaration in (("status", "TEXT NOT NULL DEFAULT 'pending'"), ("claim_token", "TEXT NOT NULL DEFAULT ''"), ("lease_until", "REAL NOT NULL DEFAULT 0")):
                self._ensure_column(conn, "webhook_deliveries", column, declaration)
            conn.execute(
                """CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS memberships (
                    user_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    PRIMARY KEY(user_id, tenant_id),
                    FOREIGN KEY(user_id) REFERENCES users(id)
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS repository_grants (
                    tenant_id TEXT NOT NULL,
                    repository TEXT NOT NULL,
                    auto_fix INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(tenant_id, repository)
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    resource TEXT NOT NULL,
                    detail_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS deployments (
                    tenant_id TEXT NOT NULL,
                    skill_name TEXT NOT NULL,
                    stable_version INTEGER,
                    candidate_version INTEGER,
                    canary_percent INTEGER NOT NULL DEFAULT 0,
                    shadow_percent INTEGER NOT NULL DEFAULT 0,
                    max_error_rate REAL NOT NULL DEFAULT 0.1,
                    min_samples INTEGER NOT NULL DEFAULT 20,
                    status TEXT NOT NULL DEFAULT 'stable',
                    samples INTEGER NOT NULL DEFAULT 0,
                    errors INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(tenant_id, skill_name)
                )"""
            )
            self._ensure_column(
                conn, "deployments", "max_disagreement_rate",
                "REAL NOT NULL DEFAULT 0.2"
            )
            self._ensure_column(
                conn, "deployments", "auto_promote", "INTEGER NOT NULL DEFAULT 0"
            )
            self._ensure_column(
                conn, "deployments", "shadow_samples", "INTEGER NOT NULL DEFAULT 0"
            )
            self._ensure_column(
                conn, "deployments", "disagreements", "INTEGER NOT NULL DEFAULT 0"
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS release_observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL,
                    skill_name TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    lane TEXT NOT NULL,
                    primary_json TEXT NOT NULL,
                    candidate_json TEXT,
                    disagreement REAL NOT NULL,
                    candidate_failed INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL,
                    alert_key TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    message TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(tenant_id, alert_key, status)
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS agent_memories (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    repository TEXT NOT NULL,
                    task_id TEXT NOT NULL DEFAULT '',
                    agent TEXT NOT NULL DEFAULT '',
                    scope TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    content TEXT NOT NULL,
                    keywords_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    importance REAL NOT NULL DEFAULT 0.5,
                    created_at TEXT NOT NULL,
                    expires_at TEXT
                )"""
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_tenant_created ON tasks(tenant_id, created_at)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_agent_memories_lookup "
                "ON agent_memories(tenant_id, repository, scope, created_at)"
            )

    @staticmethod
    def _migrate_prompt_tables(conn) -> None:
        schemas = {
            "skill_versions": (
                "id INTEGER PRIMARY KEY AUTOINCREMENT,tenant_id TEXT NOT NULL DEFAULT 'default',"
                "skill_name TEXT NOT NULL,version INTEGER NOT NULL,prompt TEXT NOT NULL,score REAL NOT NULL,"
                "active INTEGER NOT NULL DEFAULT 0,parent_version INTEGER,created_at TEXT NOT NULL,"
                "UNIQUE(tenant_id,skill_name,version)"
            ),
            "evaluation_cases": (
                "id INTEGER PRIMARY KEY AUTOINCREMENT,tenant_id TEXT NOT NULL DEFAULT 'default',"
                "name TEXT NOT NULL,split TEXT NOT NULL,diff TEXT NOT NULL,expected_json TEXT NOT NULL,"
                "source TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,"
                "UNIQUE(tenant_id,name)"
            ),
        }
        for table, schema in schemas.items():
            columns = [row[1] for row in conn.execute("PRAGMA table_info(%s)" % table)]
            if columns and "tenant_id" not in columns:
                conn.execute("ALTER TABLE %s RENAME TO %s_legacy" % (table, table))
                conn.execute("CREATE TABLE %s (%s)" % (table, schema))
                joined = ",".join(columns)
                conn.execute("INSERT INTO %s (%s) SELECT %s FROM %s_legacy" % (table, joined, joined, table))
                conn.execute("DROP TABLE %s_legacy" % table)

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(%s)" % table).fetchall()}
        if column not in columns:
            conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, declaration))
