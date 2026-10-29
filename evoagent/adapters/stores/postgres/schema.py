"""Postgres schema initialization and migrations."""



class PostgresSchemaMixin:
    def _init(self) -> None:
        statements = [
            """CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, state TEXT NOT NULL, repository TEXT NOT NULL,
                pull_request INTEGER, input_json JSONB NOT NULL, report_json JSONB,
                error TEXT, created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS trace_events (
                id BIGSERIAL PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), step INTEGER NOT NULL,
                state TEXT NOT NULL, message TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS failure_cases (
                id BIGSERIAL PRIMARY KEY, task_id TEXT NOT NULL, category TEXT NOT NULL,
                payload_json JSONB NOT NULL, resolved BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMPTZ NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS skill_versions (
                id BIGSERIAL PRIMARY KEY, skill_name TEXT NOT NULL, version INTEGER NOT NULL,
                prompt TEXT NOT NULL, score DOUBLE PRECISION NOT NULL, active BOOLEAN NOT NULL DEFAULT FALSE,
                parent_version INTEGER, created_at TIMESTAMPTZ NOT NULL, UNIQUE(skill_name, version))""",
            """CREATE TABLE IF NOT EXISTS installations (
                installation_id BIGINT PRIMARY KEY, account_login TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS evaluation_cases (
                id BIGSERIAL PRIMARY KEY, name TEXT NOT NULL UNIQUE, split TEXT NOT NULL,
                diff TEXT NOT NULL, expected_json JSONB NOT NULL, source TEXT NOT NULL,
                active BOOLEAN NOT NULL DEFAULT TRUE, created_at TIMESTAMPTZ NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS evolution_runs (
                id TEXT PRIMARY KEY, skill_name TEXT NOT NULL, candidate_version INTEGER NOT NULL,
                baseline_version INTEGER, decision TEXT NOT NULL, candidate_score DOUBLE PRECISION NOT NULL,
                baseline_score DOUBLE PRECISION NOT NULL, metrics_json JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS skill_artifact_versions (
                id BIGSERIAL PRIMARY KEY, tenant_id TEXT NOT NULL DEFAULT 'default',
                skill_name TEXT NOT NULL, version INTEGER NOT NULL,
                artifact_json JSONB NOT NULL, artifact_sha256 TEXT NOT NULL,
                score DOUBLE PRECISION NOT NULL, active BOOLEAN NOT NULL DEFAULT FALSE,
                parent_version INTEGER, created_at TIMESTAMPTZ NOT NULL,
                UNIQUE(tenant_id, skill_name, version))""",
            """CREATE TABLE IF NOT EXISTS skill_evolution_runs (
                id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL DEFAULT 'default',
                skill_name TEXT NOT NULL, candidate_version INTEGER NOT NULL,
                baseline_version INTEGER, decision TEXT NOT NULL,
                candidate_score DOUBLE PRECISION NOT NULL, baseline_score DOUBLE PRECISION NOT NULL,
                metrics_json JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL)""",
            "ALTER TABLE skill_artifact_versions ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'default'",
            "ALTER TABLE skill_evolution_runs ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'default'",
            "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'default'",
            "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS cancel_requested BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE installations ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'default'",
            "ALTER TABLE skill_versions ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'default'",
            "ALTER TABLE evaluation_cases ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'default'",
            "ALTER TABLE evolution_runs ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'default'",
            "ALTER TABLE skill_versions DROP CONSTRAINT IF EXISTS skill_versions_skill_name_version_key",
            "ALTER TABLE evaluation_cases DROP CONSTRAINT IF EXISTS evaluation_cases_name_key",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_prompt_tenant_version ON skill_versions(tenant_id,skill_name,version)",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_evaluation_tenant_name ON evaluation_cases(tenant_id,name)",
            """CREATE TABLE IF NOT EXISTS checkpoints (
                task_id TEXT NOT NULL REFERENCES tasks(id), node TEXT NOT NULL, status TEXT NOT NULL,
                attempt INTEGER NOT NULL DEFAULT 1, state_json JSONB NOT NULL, error TEXT,
                updated_at TIMESTAMPTZ NOT NULL, PRIMARY KEY(task_id,node))""",
            """CREATE TABLE IF NOT EXISTS task_payloads (
                task_id TEXT PRIMARY KEY REFERENCES tasks(id), diff TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS agent_messages (
                id BIGSERIAL PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
                sender TEXT NOT NULL, recipient TEXT NOT NULL, kind TEXT NOT NULL,
                correlation_id TEXT NOT NULL, content_json JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS webhook_deliveries (
                delivery_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, event_type TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL, task_id TEXT, received_at TIMESTAMPTZ NOT NULL)""",
            "ALTER TABLE webhook_deliveries ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'pending'",
            "ALTER TABLE webhook_deliveries ADD COLUMN IF NOT EXISTS claim_token TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE webhook_deliveries ADD COLUMN IF NOT EXISTS lease_until DOUBLE PRECISION NOT NULL DEFAULT 0",
            """CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
                active BOOLEAN NOT NULL DEFAULT TRUE, created_at TIMESTAMPTZ NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS memberships (
                user_id TEXT NOT NULL REFERENCES users(id), tenant_id TEXT NOT NULL, role TEXT NOT NULL,
                PRIMARY KEY(user_id,tenant_id))""",
            """CREATE TABLE IF NOT EXISTS schema_meta (
                key TEXT PRIMARY KEY, value TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS repository_grants (
                tenant_id TEXT NOT NULL, repository TEXT NOT NULL, auto_fix BOOLEAN NOT NULL DEFAULT FALSE,
                PRIMARY KEY(tenant_id,repository))""",
            """CREATE TABLE IF NOT EXISTS audit_log (
                id BIGSERIAL PRIMARY KEY, tenant_id TEXT NOT NULL, actor TEXT NOT NULL,
                action TEXT NOT NULL, resource TEXT NOT NULL, detail_json JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS deployments (
                tenant_id TEXT NOT NULL, skill_name TEXT NOT NULL, stable_version INTEGER,
                candidate_version INTEGER, canary_percent INTEGER NOT NULL DEFAULT 0,
                shadow_percent INTEGER NOT NULL DEFAULT 0, max_error_rate DOUBLE PRECISION NOT NULL DEFAULT .1,
                min_samples INTEGER NOT NULL DEFAULT 20, status TEXT NOT NULL DEFAULT 'stable',
                samples INTEGER NOT NULL DEFAULT 0, errors INTEGER NOT NULL DEFAULT 0,
                updated_at TIMESTAMPTZ NOT NULL, PRIMARY KEY(tenant_id,skill_name))""",
            """CREATE TABLE IF NOT EXISTS alerts (
                id BIGSERIAL PRIMARY KEY, tenant_id TEXT NOT NULL, alert_key TEXT NOT NULL,
                severity TEXT NOT NULL, message TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
                created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
                UNIQUE(tenant_id,alert_key,status))""",
            """CREATE TABLE IF NOT EXISTS agent_memories (
                id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, repository TEXT NOT NULL,
                task_id TEXT NOT NULL DEFAULT '', agent TEXT NOT NULL DEFAULT '',
                scope TEXT NOT NULL, kind TEXT NOT NULL, content TEXT NOT NULL,
                keywords_json JSONB NOT NULL, metadata_json JSONB NOT NULL,
                importance DOUBLE PRECISION NOT NULL DEFAULT .5,
                created_at TIMESTAMPTZ NOT NULL, expires_at TIMESTAMPTZ)""",
            """CREATE INDEX IF NOT EXISTS idx_agent_memories_lookup
                ON agent_memories(tenant_id,repository,scope,created_at)""",
            "ALTER TABLE deployments ADD COLUMN IF NOT EXISTS shadow_samples INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE deployments ADD COLUMN IF NOT EXISTS disagreements INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE deployments ADD COLUMN IF NOT EXISTS max_disagreement_rate DOUBLE PRECISION NOT NULL DEFAULT .2",
            "ALTER TABLE deployments ADD COLUMN IF NOT EXISTS auto_promote BOOLEAN NOT NULL DEFAULT FALSE",
            """CREATE TABLE IF NOT EXISTS release_observations (
                id BIGSERIAL PRIMARY KEY, tenant_id TEXT NOT NULL, skill_name TEXT NOT NULL,
                task_id TEXT NOT NULL, lane TEXT NOT NULL, primary_json JSONB NOT NULL,
                candidate_json JSONB, disagreement DOUBLE PRECISION NOT NULL,
                candidate_failed BOOLEAN NOT NULL DEFAULT FALSE, created_at TIMESTAMPTZ NOT NULL)""",
            "CREATE INDEX IF NOT EXISTS idx_tasks_tenant_created ON tasks(tenant_id,created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_trace_events_task ON trace_events(task_id,id)",
            "CREATE INDEX IF NOT EXISTS idx_failure_cases_task ON failure_cases(task_id)",
            "CREATE INDEX IF NOT EXISTS idx_failure_cases_resolved ON failure_cases(resolved,created_at)",
            "CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_tenant ON webhook_deliveries(tenant_id,received_at)",
            "CREATE INDEX IF NOT EXISTS idx_agent_messages_task ON agent_messages(task_id,id)",
            "CREATE INDEX IF NOT EXISTS idx_audit_log_tenant ON audit_log(tenant_id,id)",
            "CREATE INDEX IF NOT EXISTS idx_release_observations_tenant ON release_observations(tenant_id,skill_name,created_at)",
            "CREATE INDEX IF NOT EXISTS idx_alerts_tenant ON alerts(tenant_id,status,created_at)",
        ]
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(hashtext('evoagent:schema'))")
                for statement in statements:
                    cur.execute(statement)
