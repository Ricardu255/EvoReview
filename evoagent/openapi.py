"""Machine-readable HTTP contract for the EvoReview API.

The specification is intentionally hand-maintained and covered by contract
tests.  It describes the public API used by the console, scripts and external
integrations; internal Python ports are not part of this contract.
"""
from typing import Any, Dict

from . import __version__


def _ref(name: str) -> Dict[str, str]:
    return {"$ref": "#/components/schemas/" + name}


def _json(schema: Any = None, description: str = "Successful response") -> Dict[str, Any]:
    value: Dict[str, Any] = {"description": description}
    if schema is not None:
        value["content"] = {"application/json": {"schema": schema}}
    return value


def _error(description: str) -> Dict[str, Any]:
    return _json(_ref("Error"), description)


def _operation(
    operation_id: str, summary: str, tags: list, responses: Dict[str, Any],
    request: Any = None, parameters: list = None, security: Any = None,
) -> Dict[str, Any]:
    value: Dict[str, Any] = {
        "operationId": operation_id,
        "summary": summary,
        "tags": tags,
        "responses": responses,
    }
    if request is not None:
        value["requestBody"] = {
            "required": True,
            "content": {"application/json": {"schema": request}},
        }
    if parameters:
        value["parameters"] = parameters
    if security is not None:
        value["security"] = security
    return value


def _task_id_parameter() -> Dict[str, Any]:
    return {
        "name": "task_id", "in": "path", "required": True,
        "schema": {"type": "string", "format": "uuid"},
    }


def _limit_parameter(default: int = 50, maximum: int = 500) -> Dict[str, Any]:
    return {
        "name": "limit", "in": "query", "required": False,
        "schema": {
            "type": "integer", "minimum": 1, "maximum": maximum,
            "default": default,
        },
    }


def _bearer() -> list:
    return [{"bearerAuth": []}]


def _schemas() -> Dict[str, Any]:
    return {
        "Error": {
            "type": "object",
            "required": ["error"],
            "properties": {"error": {"type": "string"}},
        },
        "Health": {
            "type": "object",
            "required": ["status"],
            "properties": {"status": {"type": "string", "example": "ok"}},
        },
        "HealthDetail": {
            "type": "object",
            "required": ["status"],
            "properties": {
                "status": {"type": "string"},
                "reviewer": {"type": "string"},
                "runtime": {"type": "string"},
                "queue": {"type": "string"},
                "llm_provider": {"type": "string"},
                "llm_model": {"type": "string"},
                "run_mode": {"type": "object", "additionalProperties": True},
                "taxonomy": {"type": "object", "additionalProperties": True},
            },
        },
        "LoginRequest": {
            "type": "object",
            "required": ["username", "password"],
            "properties": {
                "username": {"type": "string"},
                "password": {"type": "string", "format": "password"},
                "tenant_id": {"type": "string"},
            },
        },
        "TokenResponse": {
            "type": "object",
            "required": ["access_token", "token_type", "expires_in", "tenant_id", "role"],
            "properties": {
                "access_token": {"type": "string"},
                "token_type": {"type": "string", "example": "Bearer"},
                "expires_in": {"type": "integer"},
                "tenant_id": {"type": "string"},
                "role": {"type": "string", "enum": ["admin", "maintainer", "auditor"]},
            },
        },
        "TokenRequest": {
            "type": "object",
            "properties": {
                "username": {"type": "string"},
                "tenant_id": {"type": "string"},
                "ttl_seconds": {"type": "integer", "minimum": 60, "maximum": 7776000},
            },
        },
        "ReviewRequest": {
            "type": "object",
            "required": ["repository", "diff"],
            "properties": {
                "repository": {"type": "string", "maxLength": 250},
                "diff": {"type": "string"},
                "pull_request": {"type": "integer", "minimum": 1},
                "mode": {"type": "string", "enum": ["agentic"]},
                "repository_root": {"type": "string"},
                "enabled_agents": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["lead", "security", "correctness-reliability", "critic"],
                    },
                },
                "enabled_skills": {"type": "array", "items": {"type": "string"}},
            },
        },
        "TaskCreateResponse": {
            "type": "object",
            "required": ["task_id", "state"],
            "properties": {
                "task_id": {"type": "string", "format": "uuid"},
                "state": {"type": "string"},
                "queue": {"type": "string"},
                "report": _ref("ReviewReport"),
            },
        },
        "Task": {
            "type": "object",
            "required": ["id", "state", "repository"],
            "properties": {
                "id": {"type": "string", "format": "uuid"},
                "state": {
                    "type": "string",
                    "enum": [
                        "PENDING", "PLANNING", "EXECUTING", "REVIEWING",
                        "SUCCESS", "FAILED", "CANCELLED",
                    ],
                },
                "repository": {"type": "string"},
                "pull_request": {"type": "integer", "nullable": True},
                "error": {"type": "string", "nullable": True},
                "created_at": {"type": "string"},
                "updated_at": {"type": "string"},
                "tenant_id": {"type": "string"},
                "input": {"type": "object", "additionalProperties": True},
                "report": _ref("ReviewReport"),
                "trace": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
                "collaboration": {
                    "type": "array",
                    "items": {"type": "object", "additionalProperties": True},
                },
            },
        },
        "Finding": {
            "type": "object",
            "required": [
                "rule_id", "severity", "title", "explanation", "path", "line",
                "evidence", "fix", "test", "confidence",
            ],
            "properties": {
                "rule_id": {"type": "string"},
                "cwe": {"type": "string", "nullable": True},
                "severity": {
                    "type": "string", "enum": ["critical", "high", "medium", "low"],
                },
                "title": {"type": "string"},
                "explanation": {"type": "string"},
                "path": {"type": "string"},
                "line": {"type": "integer", "minimum": 1},
                "evidence": {"type": "string"},
                "evidence_refs": {
                    "type": "array",
                    "items": {"type": "object", "additionalProperties": True},
                },
                "call_chain": {
                    "type": "array",
                    "items": {"type": "object", "additionalProperties": True},
                },
                "fix": {"type": "string"},
                "test": {"type": "string"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "source": {"type": "string"},
                "gate": {"type": "object", "additionalProperties": True},
            },
        },
        "ReviewReport": {
            "type": "object",
            "required": ["repository", "summary", "risk", "findings"],
            "properties": {
                "repository": {"type": "string"},
                "pull_request": {"type": "integer", "nullable": True},
                "summary": {"type": "string"},
                "risk": {"type": "string", "enum": ["low", "medium", "high"]},
                "findings": {"type": "array", "items": _ref("Finding")},
                "files_reviewed": {"type": "array", "items": {"type": "string"}},
                "reviewer": {"type": "string"},
                "collaboration": {"type": "object", "additionalProperties": True},
                "run_mode": {"type": "object", "additionalProperties": True},
                "components": {
                    "type": "array",
                    "items": {"type": "object", "additionalProperties": True},
                },
                "execution": {"type": "object", "additionalProperties": True},
            },
        },
        "FeedbackRequest": {
            "type": "object",
            "required": ["category"],
            "properties": {
                "category": {
                    "type": "string",
                    "enum": ["false_positive", "missed_issue", "bad_fix", "accepted"],
                },
                "finding": {"type": "object", "nullable": True, "additionalProperties": True},
                "note": {"type": "string", "maxLength": 2000},
            },
        },
        "FixRequest": {
            "type": "object",
            "properties": {"installation_id": {"type": "integer"}},
        },
        "Grant": {
            "type": "object",
            "required": ["repository", "auto_fix"],
            "properties": {
                "repository": {"type": "string"},
                "auto_fix": {"type": "boolean"},
            },
        },
        "User": {
            "type": "object",
            "required": ["username", "role", "active"],
            "properties": {
                "username": {"type": "string"},
                "role": {"type": "string"},
                "active": {"type": "boolean"},
                "platform_admin": {"type": "boolean"},
                "created_at": {"type": "string"},
            },
        },
        "UserCreateRequest": {
            "type": "object",
            "required": ["username", "password"],
            "properties": {
                "username": {"type": "string", "maxLength": 255},
                "password": {"type": "string", "minLength": 10},
                "role": {"type": "string", "enum": ["admin", "maintainer", "auditor"]},
                "tenant_id": {"type": "string"},
            },
        },
        "Tenant": {
            "type": "object",
            "required": ["tenant_id"],
            "properties": {
                "tenant_id": {"type": "string"},
                "created_at": {"type": "string"},
            },
        },
        "Alert": {
            "type": "object",
            "required": ["tenant_id", "alert_key", "severity", "message", "status"],
            "properties": {
                "id": {"type": "integer"},
                "tenant_id": {"type": "string"},
                "alert_key": {"type": "string"},
                "severity": {"type": "string"},
                "message": {"type": "string"},
                "status": {"type": "string"},
                "created_at": {"type": "string"},
                "updated_at": {"type": "string"},
            },
        },
        "AuditEvent": {
            "type": "object",
            "required": ["actor", "action", "resource"],
            "properties": {
                "actor": {"type": "string"},
                "action": {"type": "string"},
                "resource": {"type": "string"},
                "detail": {"type": "object", "additionalProperties": True},
                "created_at": {"type": "string"},
            },
        },
        "DeadLetter": {
            "type": "object",
            "properties": {
                "message_id": {"type": "string"},
                "attempt": {"type": "integer"},
                "payload": {"type": "object", "additionalProperties": True},
                "error": {"type": "string"},
                "failed_at": {"type": "number"},
            },
        },
        "EvaluationCase": {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "name": {"type": "string"},
                "split": {"type": "string", "enum": ["train", "validation", "holdout"]},
                "diff": {"type": "string"},
                "expected": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
                "active": {"type": "boolean"},
            },
        },
        "Skill": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "version": {"type": "string"},
                "description": {"type": "string"},
                "source": {"type": "string"},
                "kind": {"type": "string", "enum": ["scanner", "agent-skill"]},
                "sandboxed": {"type": "boolean"},
                "permissions": {"type": "array", "items": {"type": "string"}},
                "resources": {"type": "array", "items": {"type": "string"}},
                "content_sha256": {"type": "string"},
            },
        },
        "PromptVersion": {
            "type": "object",
            "properties": {
                "version": {"type": "integer"},
                "score": {"type": "number"},
                "active": {"type": "boolean"},
                "parent_version": {"type": "integer", "nullable": True},
                "created_at": {"type": "string"},
                "prompt_sha256": {"type": "string"},
                "prompt_preview": {"type": "string"},
            },
        },
        "Dashboard": {
            "type": "object",
            "properties": {
                "stats": {"type": "object", "additionalProperties": True},
                "tasks": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
                "queue": {"type": "string"},
                "orchestrator": {"type": "string"},
                "llm": {"type": "object", "additionalProperties": True},
                "run_mode": {"type": "object", "additionalProperties": True},
                "taxonomy": {"type": "object", "additionalProperties": True},
            },
        },
    }


def _paths() -> Dict[str, Any]:
    task_responses = {
        "200": _json(_ref("Task")),
        "401": _error("Authentication required"),
        "403": _error("Permission denied"),
        "404": _error("Task not found"),
    }
    return {
        "/openapi.json": {
            "get": _operation(
                "getOpenApi", "Get this OpenAPI document", ["meta"],
                {"200": _json({"type": "object", "additionalProperties": True})},
                security=[],
            )
        },
        "/health": {
            "get": _operation(
                "getHealth", "Unauthenticated liveness check", ["meta"],
                {"200": _json(_ref("Health"))}, security=[],
            )
        },
        "/health/detail": {
            "get": _operation(
                "getHealthDetail", "Authenticated deployment details", ["meta"],
                {"200": _json(_ref("HealthDetail")), "401": _error("Authentication required")},
            )
        },
        "/metrics": {
            "get": _operation(
                "getMetrics", "Prometheus text metrics", ["meta"],
                {
                    "200": {"description": "Prometheus exposition", "content": {
                        "text/plain": {"schema": {"type": "string"}},
                    }},
                    "401": _error("Authentication required"),
                },
            )
        },
        "/v1/auth/login": {
            "post": _operation(
                "login", "Log in and obtain a tenant-bound Bearer token", ["auth"],
                {
                    "200": _json(_ref("TokenResponse")),
                    "401": _error("Invalid credentials"),
                    "409": _error("Authentication is disabled"),
                },
                request=_ref("LoginRequest"), security=[],
            )
        },
        "/v1/auth/token": {
            "post": _operation(
                "issueToken", "Issue a long-lived tenant-bound Bearer token", ["auth"],
                {
                    "201": _json(_ref("TokenResponse")),
                    "400": _error("Invalid request"),
                    "401": _error("Authentication required"),
                    "403": _error("Permission denied"),
                    "409": _error("Authentication is disabled"),
                },
                request=_ref("TokenRequest"),
            )
        },
        "/v1/reviews": {
            "post": _operation(
                "createReview", "Create a synchronous or asynchronous review task", ["reviews"],
                {
                    "201": _json(_ref("TaskCreateResponse")),
                    "202": _json(_ref("TaskCreateResponse")),
                    "400": _error("Invalid review request"),
                    "401": _error("Authentication required"),
                    "403": _error("Permission denied"),
                    "503": _error("Model not configured"),
                },
                request=_ref("ReviewRequest"),
                parameters=[{
                    "name": "async", "in": "query", "required": False,
                    "schema": {"type": "boolean", "default": False},
                }],
            )
        },
        "/webhooks/github": {
            "post": _operation(
                "githubWebhook", "Receive a GitHub pull_request webhook", ["github"],
                {
                    "202": _json({"type": "object", "additionalProperties": True}),
                    "400": _error("Invalid payload"),
                    "401": _error("Invalid signature"),
                    "403": _error("Repository or installation not authorized"),
                    "503": _error("Webhook secret is not configured"),
                },
                request={"type": "object", "additionalProperties": True},
                security=[],
            )
        },
        "/v1/tasks/{task_id}": {
            "get": _operation(
                "getTask", "Get task state, execution record and report", ["tasks"],
                task_responses, parameters=[_task_id_parameter()],
            )
        },
        "/v1/tasks/{task_id}/report": {
            "get": _operation(
                "getTaskReport", "Get the Markdown review report", ["tasks"],
                {
                    "200": {"description": "Markdown report", "content": {
                        "text/markdown": {"schema": {"type": "string"}},
                    }},
                    "404": _error("Task or report not found"),
                },
                parameters=[_task_id_parameter()],
            )
        },
        "/v1/tasks/{task_id}/feedback": {
            "get": _operation(
                "getTaskFeedback", "List feedback attached to a task", ["tasks"],
                {"200": _json({"type": "object", "additionalProperties": True})},
                parameters=[_task_id_parameter()],
            ),
            "post": _operation(
                "recordTaskFeedback", "Record false-positive, missed-issue or bad-fix feedback", ["tasks"],
                {
                    "201": _json({"type": "object", "additionalProperties": True}),
                    "400": _error("Invalid feedback"),
                    "404": _error("Task not found"),
                },
                request=_ref("FeedbackRequest"), parameters=[_task_id_parameter()],
            ),
        },
        "/v1/tasks/{task_id}/fix": {
            "post": _operation(
                "createFix", "Create a verified repair branch and Draft PR", ["fixes"],
                {
                    "201": _json({"type": "object", "additionalProperties": True}),
                    "400": _error("Invalid request"),
                    "403": _error("Automatic repair is not enabled"),
                    "404": _error("Task not found"),
                },
                request=_ref("FixRequest"), parameters=[_task_id_parameter()],
            )
        },
        "/v1/tasks/{task_id}/cancel": {
            "post": _operation(
                "cancelTask", "Request task cancellation", ["tasks"],
                {"202": _json({"type": "object", "additionalProperties": True}),
                 "404": _error("Task not found")},
                parameters=[_task_id_parameter()],
            )
        },
        "/v1/tasks/{task_id}/resume": {
            "post": _operation(
                "resumeTask", "Resume a failed or cancelled task", ["tasks"],
                {"202": _json({"type": "object", "additionalProperties": True}),
                 "404": _error("Task not found")},
                parameters=[_task_id_parameter()],
            )
        },
        "/v1/tasks/{task_id}/comment": {
            "post": _operation(
                "redeliverComment", "Re-post the stored GitHub review comment", ["tasks"],
                {"200": _json({"type": "object", "additionalProperties": True}),
                 "502": _error("GitHub delivery failed")},
                parameters=[_task_id_parameter()],
            )
        },
        "/v1/repository-grants": {
            "get": _operation(
                "listRepositoryGrants", "List tenant repository grants", ["admin"],
                {"200": _json({"type": "object", "additionalProperties": True})},
            ),
            "post": _operation(
                "grantRepository", "Grant review or auto-fix access to a repository", ["admin"],
                {"201": _json(_ref("Grant")), "400": _error("Invalid grant")},
                request=_ref("Grant"),
            ),
            "delete": _operation(
                "revokeRepository", "Revoke a tenant repository grant", ["admin"],
                {"200": _json({"type": "object", "additionalProperties": True}),
                 "404": _error("Grant not found")},
                parameters=[{
                    "name": "repository", "in": "query", "required": True,
                    "schema": {"type": "string"},
                }],
            ),
        },
        "/v1/users": {
            "get": _operation(
                "listUsers", "List users in a tenant", ["admin"],
                {"200": _json({"type": "object", "additionalProperties": True})},
                parameters=[{
                    "name": "tenant_id", "in": "query", "required": False,
                    "schema": {"type": "string"},
                }],
            ),
            "post": _operation(
                "createUser", "Create a user or update a tenant membership", ["admin"],
                {"201": _json({"type": "object", "additionalProperties": True}),
                 "400": _error("Invalid user")},
                request=_ref("UserCreateRequest"),
            ),
        },
        "/v1/users/{username}/password": {
            "post": _operation(
                "resetUserPassword", "Reset a tenant user's password", ["admin"],
                {"200": _json({"type": "object", "additionalProperties": True}),
                 "404": _error("User not found")},
                request={"type": "object", "required": ["password"],
                         "properties": {"password": {"type": "string", "minLength": 10}}},
                parameters=[{"name": "username", "in": "path", "required": True,
                             "schema": {"type": "string"}}],
            )
        },
        "/v1/users/{username}/active": {
            "post": _operation(
                "setUserActive", "Enable or disable a tenant user", ["admin"],
                {"200": _json({"type": "object", "additionalProperties": True}),
                 "404": _error("User not found")},
                request={"type": "object", "required": ["active"],
                         "properties": {"active": {"type": "boolean"}}},
                parameters=[{"name": "username", "in": "path", "required": True,
                             "schema": {"type": "string"}}],
            )
        },
        "/v1/tenants": {
            "get": _operation(
                "listTenants", "List tenants (platform scope)", ["admin"],
                {"200": _json({"type": "object", "additionalProperties": True})},
            ),
            "post": _operation(
                "createTenant", "Create a tenant (platform scope)", ["admin"],
                {"201": _json(_ref("Tenant")), "400": _error("Invalid tenant"),
                 "409": _error("Tenant already exists")},
                request={"type": "object", "required": ["tenant_id"],
                         "properties": {"tenant_id": {"type": "string", "maxLength": 100}}},
            ),
        },
        "/v1/platform/users/{username}/platform-admin": {
            "post": _operation(
                "setPlatformAdmin", "Grant or revoke platform scope", ["admin"],
                {"200": _json({"type": "object", "additionalProperties": True}),
                 "404": _error("User not found")},
                request={"type": "object", "required": ["active"],
                         "properties": {"active": {"type": "boolean"}}},
                parameters=[{"name": "username", "in": "path", "required": True,
                             "schema": {"type": "string"}}],
            )
        },
        "/v1/skills/reload": {
            "post": _operation(
                "reloadSkills", "Reload Agent Skills from disk", ["skills"],
                {"200": _json({"type": "object", "additionalProperties": True})},
            )
        },
        "/v1/skills/{name}/versions": {
            "get": _operation(
                "listPromptVersions", "List prompt versions for a tenant", ["skills"],
                {"200": _json({"type": "object", "additionalProperties": True})},
                parameters=[{"name": "name", "in": "path", "required": True,
                             "schema": {"type": "string"}}],
            )
        },
        "/v1/skills/{name}/versions/{version}/activate": {
            "post": _operation(
                "activatePromptVersion", "Activate or roll back a prompt version", ["skills"],
                {"200": _json({"type": "object", "additionalProperties": True}),
                 "404": _error("Version not found")},
                parameters=[
                    {"name": "name", "in": "path", "required": True,
                     "schema": {"type": "string"}},
                    {"name": "version", "in": "path", "required": True,
                     "schema": {"type": "integer"}},
                ],
            )
        },
        "/v1/evolution/auto": {
            "post": _operation(
                "autoEvolvePrompt", "Generate and evaluate a prompt candidate", ["evolution"],
                {"201": _json({"type": "object", "additionalProperties": True})},
                request={"type": "object", "properties": {"skill_name": {"type": "string"}}},
            )
        },
        "/v1/evolution/propose": {
            "post": _operation(
                "proposePrompt", "Evaluate a prompt candidate", ["evolution"],
                {"201": _json({"type": "object", "additionalProperties": True})},
                request={"type": "object", "required": ["skill_name", "prompt"],
                         "properties": {
                             "skill_name": {"type": "string"},
                             "prompt": {"type": "string"},
                             "regression_score": {"type": "number"},
                         }},
            )
        },
        "/v1/evolution/status": {
            "get": _operation(
                "getEvolutionStatus", "Prompt evaluation readiness", ["evolution"],
                {"200": _json({"type": "object", "additionalProperties": True})},
            )
        },
        "/v1/evolution/runs": {
            "get": _operation(
                "listEvolutionRuns", "List persisted prompt evolution runs", ["evolution"],
                {"200": _json({"type": "object", "additionalProperties": True})},
                parameters=[_limit_parameter(50, 200)],
            )
        },
        "/v1/skill-evolution/auto": {
            "post": _operation(
                "autoEvolveSkill", "Generate and gate an Agent Skill candidate", ["evolution"],
                {"201": _json({"type": "object", "additionalProperties": True})},
                request={"type": "object", "properties": {"skill_name": {"type": "string"}}},
            )
        },
        "/v1/skill-evolution/propose": {
            "post": _operation(
                "proposeSkill", "Evaluate an Agent Skill artifact", ["evolution"],
                {"201": _json({"type": "object", "additionalProperties": True})},
                request={"type": "object", "additionalProperties": True},
            )
        },
        "/v1/skill-evolution/status": {
            "get": _operation(
                "getSkillEvolutionStatus", "Agent Skill gate and active version", ["evolution"],
                {"200": _json({"type": "object", "additionalProperties": True})},
                parameters=[{"name": "skill_name", "in": "query", "required": False,
                             "schema": {"type": "string"}}],
            )
        },
        "/v1/skill-evolution/runs": {
            "get": _operation(
                "listSkillEvolutionRuns", "List Agent Skill evolution runs", ["evolution"],
                {"200": _json({"type": "object", "additionalProperties": True})},
                parameters=[_limit_parameter(50, 200)],
            )
        },
        "/v1/skill-evolution/{name}/versions": {
            "get": _operation(
                "listSkillVersions", "List Agent Skill artifact versions", ["evolution"],
                {"200": _json({"type": "object", "additionalProperties": True})},
                parameters=[{"name": "name", "in": "path", "required": True,
                             "schema": {"type": "string"}}],
            )
        },
        "/v1/skill-evolution/{name}/versions/{version}/activate": {
            "post": _operation(
                "activateSkillVersion", "Activate or roll back an Agent Skill artifact", ["evolution"],
                {"200": _json({"type": "object", "additionalProperties": True}),
                 "404": _error("Version not found")},
                parameters=[
                    {"name": "name", "in": "path", "required": True,
                     "schema": {"type": "string"}},
                    {"name": "version", "in": "path", "required": True,
                     "schema": {"type": "integer"}},
                ],
            )
        },
        "/v1/evaluation/cases": {
            "get": _operation(
                "listEvaluationCases", "List evaluation cases", ["evolution"],
                {"200": _json({"type": "object", "additionalProperties": True}),
                 "403": _error("Holdout cases are not exposed")},
                parameters=[{
                    "name": "split", "in": "query", "required": False,
                    "schema": {"type": "string", "enum": ["train", "validation"]},
                }],
            ),
            "post": _operation(
                "addEvaluationCase", "Add an evaluation case", ["evolution"],
                {"201": _json(_ref("EvaluationCase")), "400": _error("Invalid case")},
                request={"type": "object", "required": ["name", "diff", "expected_findings"],
                         "properties": {
                             "name": {"type": "string"},
                             "diff": {"type": "string"},
                             "expected_findings": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
                             "split": {"type": "string", "enum": ["train", "validation", "holdout"]},
                         }},
            )
        },
        "/api/dashboard": {
            "get": _operation(
                "getDashboard", "Dashboard stats and recent tasks", ["console"],
                {"200": _json(_ref("Dashboard"))},
            )
        },
        "/api/tasks": {
            "get": _operation(
                "listTasks", "List tenant tasks", ["console"],
                {"200": _json({"type": "object", "additionalProperties": True})},
                parameters=[
                    _limit_parameter(50, 200),
                    {"name": "state", "in": "query", "required": False,
                     "schema": {"type": "string"}},
                    {"name": "repository", "in": "query", "required": False,
                     "schema": {"type": "string"}},
                ],
            )
        },
        "/api/skills": {
            "get": _operation(
                "listSkills", "List scanners and active Agent Skills", ["console"],
                {"200": _json({"type": "object", "additionalProperties": True})},
            )
        },
        "/api/failures": {
            "get": _operation(
                "listFailures", "List unresolved failure cases", ["console"],
                {"200": _json({"type": "object", "additionalProperties": True}),
                 "403": _error("Permission denied")},
            )
        },
        "/api/audit": {
            "get": _operation(
                "listAudit", "List tenant audit events", ["console"],
                {"200": _json({"type": "object", "additionalProperties": True}),
                 "403": _error("Permission denied")},
                parameters=[_limit_parameter(100, 500)],
            )
        },
        "/api/alerts": {
            "get": _operation(
                "listAlerts", "List tenant alerts", ["console"],
                {"200": _json({"type": "object", "additionalProperties": True})},
            )
        },
        "/api/alerts/{id}/resolve": {
            "post": _operation(
                "resolveAlert", "Resolve an open alert", ["console"],
                {"200": _json({"type": "object", "additionalProperties": True}),
                 "404": _error("Alert not found")},
                parameters=[{"name": "id", "in": "path", "required": True,
                             "schema": {"type": "integer", "minimum": 1}}],
            )
        },
        "/api/deployments/llm-review": {
            "get": _operation(
                "getDeployment", "Get canary/shadow deployment state", ["console"],
                {"200": _json({"type": "object", "additionalProperties": True})},
            )
        },
        "/v1/deployments/llm-review": {
            "post": _operation(
                "configureDeployment", "Configure canary/shadow rollout", ["console"],
                {"201": _json({"type": "object", "additionalProperties": True})},
                request={"type": "object", "additionalProperties": True},
            )
        },
        "/api/queue/dead-letters": {
            "get": _operation(
                "listDeadLetters", "List dead-letter deliveries", ["console"],
                {"200": _json({"type": "object", "additionalProperties": True}),
                 "403": _error("Permission denied")},
                parameters=[_limit_parameter(100, 500)],
            )
        },
        "/v1/queue/dead-letters/replay": {
            "post": _operation(
                "replayDeadLetter", "Replay a dead-letter delivery", ["console"],
                {"202": _json({"type": "object", "additionalProperties": True}),
                 "404": _error("Dead letter not found")},
                request={"type": "object", "required": ["message_id"],
                         "properties": {"message_id": {"type": "string"}}},
            )
        },
    }


def build_openapi() -> Dict[str, Any]:
    return {
        "openapi": "3.0.3",
        "info": {
            "title": "EvoReview API",
            "version": __version__,
            "description": (
                "External HTTP contract for the EvoReview review, tenant, "
                "evolution and operations APIs."
            ),
        },
        "servers": [{"url": "/"}],
        "tags": [
            {"name": "meta"}, {"name": "auth"}, {"name": "reviews"},
            {"name": "tasks"}, {"name": "fixes"}, {"name": "skills"},
            {"name": "evolution"}, {"name": "admin"}, {"name": "console"},
            {"name": "github"},
        ],
        "security": _bearer(),
        "components": {
            "securitySchemes": {
                "bearerAuth": {
                    "type": "http", "scheme": "bearer",
                    "bearerFormat": "JWT",
                }
            },
            "schemas": _schemas(),
        },
        "paths": _paths(),
    }
