import hashlib
import json
import os
import threading
import uuid
from typing import Any, Dict, Optional

from .agentic_core import AgenticReviewer
from .auth import AuthManager
from .config import Settings
from .context_manager import ContextManager
from .evolution import EvolutionEngine
from .evolution_v2 import RootCauseEvolutionGenerator
from .finding_identity import canonical_identity
from .fixer import SafeFixer
from .patching import SuggestionOnlyFixer, VerifiedPatchFixer
from .github import GitHubAppAuthenticator, GitHubClient
from .harness import ReviewHarness
from .metrics import metrics
from .llm import JsonChatClient
from .modes import RunMode
from .memory import MemoryManager
from .models import TaskState, TraceEvent
from .observability import AlertManager, Observability
from .postgres_store import create_store
from .report import to_markdown
from .reviewer import (
    OpenAICompatibleReviewer, ReliabilityRuleReviewer, SecurityRuleReviewer,
)
from .skills import AgentSkill, SkillRegistry
from .skill_evolution import AgentSkillReplayReviewer, SkillEvolutionEngine, validate_artifact
from .time_utils import utc_now
from .task_queue import PermanentTaskError, TaskQueue
from .rollout import ReleaseManager
from .verifier import RepairVerifier


class ReviewService:
    def __init__(self, settings: Settings):
        self.settings = settings
        settings.validate_evolution()
        self.repository_roots = json.loads(settings.repository_roots_json)
        if not isinstance(self.repository_roots, dict) or any(
            not isinstance(repositories, dict) or any(
                not isinstance(path, str) or not os.path.isabs(path)
                for path in repositories.values()
            ) for repositories in self.repository_roots.values()
        ):
            raise ValueError("repository roots must map tenant IDs to repository/absolute-path mappings")
        self._reload_lock = threading.Lock()
        self.llm_config = settings.resolved_llm()
        self.store = create_store(settings.database_url, settings.db_path)
        self.memory = MemoryManager(
            self.store, settings.memory_enabled, settings.memory_recall_limit,
            settings.memory_working_ttl_seconds,
        )
        self.context_manager = ContextManager(
            settings.agent_context_window_tokens,
            settings.agent_context_input_tokens,
            settings.context_diff_token_budget,
            settings.context_observation_token_budget,
            settings.context_recent_observations,
            settings.context_map_chunk_tokens,
        )
        self.observability = Observability(settings.otel_service_name, settings.otel_endpoint)
        self.registry = SkillRegistry(settings.skills_dir)
        self.registry.register(
            "security-review", SecurityRuleReviewer(),
            "1.0.0", "Security, injection and secret detection",
        )
        self.registry.register(
            "reliability-review", ReliabilityRuleReviewer(),
            "1.0.0", "Reliability and observability review",
        )
        if self.llm_config:
            active = self.store.get_active_skill_version("llm-review", self.settings.default_tenant_id)
            self.registry.register(
                "llm-review",
                self._build_llm_reviewer(active["prompt"] if active else ""),
                "1.0.0", "Context-aware AI code review via %s" % self.llm_config["provider"],
            )
        self.registry.reload()
        self.chat_client = (
            JsonChatClient(
                str(self.llm_config["base_url"]), str(self.llm_config["api_key"]),
                str(self.llm_config["model"]), str(self.llm_config["provider"]),
                settings.timeout_seconds, dict(self.llm_config.get("headers") or {}),
                retries=settings.llm_max_retries,
                backoff_seconds=settings.llm_retry_backoff_seconds,
            ) if self.llm_config else None
        )
        self.reviewer = self._build_agentic_reviewer()
        self.harness = ReviewHarness(
            self.store, self.reviewer, settings.max_steps, settings.timeout_seconds,
            observability=self.observability,
        )
        self.github = GitHubClient(settings.github_token)
        repair_verifier = RepairVerifier(
            settings.repair_test_command, settings.repair_verify_timeout_seconds
        )
        self.fixer = (
            VerifiedPatchFixer(self.chat_client, repair_verifier)
            if self.chat_client else SuggestionOnlyFixer()
        )
        self.auth = AuthManager(
            self.store, settings.auth_secret, settings.session_ttl_seconds,
            settings.bootstrap_admin_username, settings.bootstrap_admin_password,
            settings.default_tenant_id,
        )
        self.releases = ReleaseManager(self.store)
        self.alerts = AlertManager(
            self.store, settings.alert_failure_rate, settings.alert_min_samples
        )
        self.evolution = EvolutionEngine(
            self.store,
            reviewer_factory=self._build_llm_reviewer if self.llm_config else None,
            min_cases=settings.eval_min_cases,
            max_cases=settings.eval_max_cases,
            min_improvement=settings.eval_min_improvement,
            min_holdout_cases=settings.eval_min_holdout_cases,
            max_metric_regression=settings.eval_max_metric_regression,
            candidate_generator=(
                RootCauseEvolutionGenerator(self.chat_client)
                if self.chat_client else None
            ),
            tenant_id=settings.default_tenant_id,
        )
        self.skill_evolution = SkillEvolutionEngine(
            self.store,
            reviewer_factory=(lambda artifact: AgentSkillReplayReviewer(
                    artifact, self.chat_client,
                    settings.agent_token_budget,
                    settings.agent_time_budget_seconds,
                )) if self.chat_client else None,
            min_cases=settings.eval_min_cases,
            max_cases=settings.eval_max_cases,
            min_improvement=settings.eval_min_improvement,
            min_holdout_cases=settings.eval_min_holdout_cases,
            max_metric_regression=settings.eval_max_metric_regression,
        )
        self._tenant_evolutions = {settings.default_tenant_id: self.evolution}
        self.queue = TaskQueue(
            self._process_queued, settings.async_workers, settings.redis_url,
            settings.queue_max_attempts, settings.queue_lease_seconds,
            self._on_dead_letter,
        )

    def _build_llm_reviewer(self, prompt: str = "") -> OpenAICompatibleReviewer:
        if not self.llm_config:
            raise RuntimeError("no LLM provider is configured")
        return OpenAICompatibleReviewer(
            str(self.llm_config["base_url"]),
            str(self.llm_config["api_key"]),
            str(self.llm_config["model"]),
            self.settings.timeout_seconds,
            system_prompt=prompt,
            provider=str(self.llm_config["provider"]),
            extra_headers=dict(self.llm_config.get("headers") or {}),
        )

    def evolution_for(self, tenant_id: str) -> EvolutionEngine:
        with self._reload_lock:
            if tenant_id not in self._tenant_evolutions:
                self._tenant_evolutions[tenant_id] = self.evolution.for_tenant(tenant_id)
            return self._tenant_evolutions[tenant_id]

    def _build_agentic_reviewer(
        self, tenant_id: Optional[str] = None, prompt_version: Optional[int] = None,
        shadow: bool = False,
    ) -> AgenticReviewer:
        tenant_id = tenant_id or self.settings.default_tenant_id
        enabled = {
            item.strip() for item in self.settings.enabled_agents.split(",") if item.strip()
        }
        unknown = enabled.difference({
            "lead", "security", "correctness-reliability", "critic"
        })
        if unknown:
            raise ValueError("unsupported enabled Agent role(s): %s" % ", ".join(sorted(unknown)))
        active_prompt = (
            self.store.get_active_skill_version("llm-review", tenant_id)
            if prompt_version is None else
            self.store.get_skill_version("llm-review", prompt_version, tenant_id)
            if prompt_version else None
        )
        if prompt_version and not active_prompt:
            raise ValueError("the task's pinned prompt version is unavailable")
        structured_config = {}
        if active_prompt:
            run = next((
                item for item in self.store.list_evolution_runs(200, tenant_id)
                if item["skill_name"] == "llm-review"
                if int(item.get("candidate_version") or 0) == int(active_prompt["version"])
                and (item.get("metrics") or {}).get("structured_candidate")
            ), None)
            if run:
                structured_config = (
                    run["metrics"]["structured_candidate"].get("candidate") or {}
                )
        return AgenticReviewer(
            self.store, self.chat_client,
            self.settings.agent_token_budget,
            self.settings.agent_time_budget_seconds,
            self.settings.llm_input_cost_per_million,
            self.settings.llm_output_cost_per_million,
            enabled,
            [
                item for item in self.registry.reviewers()
                if not isinstance(item, OpenAICompatibleReviewer)
            ],
            None,
            self.settings.repair_test_command,
            active_prompt["prompt"] if active_prompt else "",
            structured_config,
            None if shadow else self.memory,
            self.context_manager,
            self._active_agent_skills,
        )

    def _run_review(
        self, task_id: str, repository: str, pull_request: Optional[int],
        diff: str, tenant_id: str,
    ):
        task = self.store.get(task_id, tenant_id)
        if not task:
            raise ValueError("task not found")
        task_input = task.get("input") or {}
        if "prompt_version" not in task_input:
            task_input.update(self._execution_assignment(tenant_id, task_id))
            self.store.update_task_input(task_id, task_input)
        reviewer = self._build_agentic_reviewer(tenant_id, task_input["prompt_version"])
        harness = ReviewHarness(
            self.store, reviewer, self.settings.max_steps, self.settings.timeout_seconds,
            observability=self.observability,
        )
        report = harness.run(task_id, repository, pull_request, diff, tenant_id)
        if task_input.get("shadow"):
            try:
                self._run_shadow(task_id, repository, pull_request, diff, tenant_id, task_input, report)
            except Exception:
                # Shadow infrastructure must not change a successful primary result.
                metrics.inc("shadow_observation_errors_total")
        return report

    def _execution_assignment(self, tenant_id: str, task_id: str) -> dict:
        assignment = self.releases.assignment(tenant_id, "llm-review", task_id)
        active = self.store.get_active_skill_version("llm-review", tenant_id)
        return {
            "release_lane": assignment["lane"], "shadow": assignment["shadow"],
            "prompt_version": assignment.get("prompt_version", active["version"] if active else 0),
            "shadow_version": assignment.get("shadow_version"),
        }

    @staticmethod
    def _release_result(report) -> dict:
        return {"prompt_version": report.execution.get("prompt_version"), "finding_keys": sorted({
            json.dumps([canonical_identity(item.rule_id, item.cwe), item.path, item.line])
            for item in report.findings
        })}

    def _run_shadow(self, task_id, repository, pull_request, diff, tenant_id, task_input, primary):
        shadow_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "evoagent:shadow:" + task_id))
        if self.store.load_checkpoints(task_id).get("shadow-observed", {}).get("status") == "completed":
            return
        candidate, failed = None, False
        if not self.store.get(shadow_id, tenant_id):
            self.store.create(shadow_id, repository, pull_request, {
                **task_input, "source": "shadow", "shadow": False, "primary_task_id": task_id,
                "prompt_version": task_input["shadow_version"],
            }, tenant_id)
            self.store.save_task_payload(shadow_id, diff)
        try:
            reviewer = self._build_agentic_reviewer(tenant_id, task_input["shadow_version"], shadow=True)
            report = ReviewHarness(
                self.store, reviewer, self.settings.max_steps, self.settings.timeout_seconds,
                observability=self.observability,
            ).run(shadow_id, repository, pull_request, diff, tenant_id)
            candidate = self._release_result(report)
        except Exception:
            failed = True
        self.releases.observe_shadow(
            tenant_id, "llm-review", task_id, task_input["release_lane"],
            self._release_result(primary), candidate, failed,
        )
        self.store.save_checkpoint(task_id, "shadow-observed", {"task_id": shadow_id, "failed": failed})

    def reload_skills(self) -> list:
        with self._reload_lock:
            if self.llm_config:
                active = self.store.get_active_skill_version("llm-review", self.settings.default_tenant_id)
                self.registry.register(
                    "llm-review",
                    self._build_llm_reviewer(active["prompt"] if active else ""),
                    "1.0.0", "Context-aware AI code review via %s" % self.llm_config["provider"],
                )
            self.registry.reload()
            skills = self.registry.list()
            reviewer = self._build_agentic_reviewer()
            harness = ReviewHarness(
                self.store, reviewer, self.settings.max_steps, self.settings.timeout_seconds,
                observability=self.observability,
            )
            # Swap only after both objects exist so a request thread always sees
            # a harness built around the reviewer it will actually execute.
            self.reviewer = reviewer
            self.harness = harness
            return skills

    def _active_agent_skills(self, tenant_id: str) -> list:
        values = {skill.name: skill for skill in self.registry.agent_skills()}
        for version in self.store.list_active_skill_artifacts(tenant_id):
            artifact = validate_artifact(version["artifact"], version["skill_name"])
            values[version["skill_name"]] = AgentSkill.from_artifact(
                artifact, str(version["version"])
            )
        return [values[name] for name in sorted(values)]

    def list_skills(self, tenant_id: str) -> list:
        scanners = [item for item in self.registry.list() if item.get("kind") == "scanner"]
        return scanners + [{
            "name": skill.name, "version": skill.version,
            "description": skill.description, "source": skill.source,
            "kind": "agent-skill", "sandboxed": False,
            "permissions": list(skill.allowed_tools),
            "content_sha256": skill.content_sha256,
            "resources": list(skill.resource_paths),
        } for skill in self._active_agent_skills(tenant_id)]

    def _validate_review(self, repository: str, diff: str) -> None:
        if not repository or len(repository) > 250:
            raise ValueError("repository is required and must be at most 250 characters")
        size = len(diff.encode("utf-8"))
        if size == 0:
            raise ValueError("diff is required")
        if size > self.settings.max_diff_bytes:
            raise ValueError("diff exceeds maximum size of %d bytes" % self.settings.max_diff_bytes)

    def _require_agentic_model(self) -> None:
        if self.chat_client is None:
            raise RuntimeError("agentic review requires a configured model")

    def _create_task(
        self, repository: str, diff: str, pull_request: Optional[int], source: str,
        tenant_id: str = "default", repository_root: str = "",
        enabled_agents: Optional[list] = None,
        enabled_skills: Optional[list] = None,
    ) -> str:
        task_id = str(uuid.uuid4())
        encoded = diff.encode("utf-8")
        assignment = self._execution_assignment(tenant_id, task_id)
        self.store.create(task_id, repository, pull_request, {
            "source": source, "diff_bytes": len(encoded), "diff_sha256": hashlib.sha256(encoded).hexdigest(),
            **assignment,
            "mode": RunMode.AGENTIC.value,
            "repository_root": repository_root,
            "enabled_agents": enabled_agents or [],
            "enabled_skills": enabled_skills or [],
        }, tenant_id)
        self.store.save_task_payload(task_id, diff)
        return task_id

    def _create_deferred_task(
        self, repository: str, pull_request: Optional[int], source: str,
        tenant_id: str, payload: Dict[str, Any], task_id: Optional[str] = None,
    ) -> str:
        task_id = task_id or str(uuid.uuid4())
        assignment = self._execution_assignment(tenant_id, task_id)
        self.store.create(task_id, repository, pull_request, {
            "source": source, "diff_pending": True,
            "repository_root": self._resolve_repository_root(repository, "", tenant_id),
            **assignment,
            **payload,
            "mode": RunMode.AGENTIC.value,
        }, tenant_id)
        return task_id

    def create_review(
        self, repository: str, diff: str, pull_request: Optional[int] = None,
        source: str = "api", tenant_id: str = "default", mode: str = "",
        repository_root: str = "", enabled_agents: Optional[list] = None,
        enabled_skills: Optional[list] = None,
    ) -> Dict[str, Any]:
        self._validate_review(repository, diff)
        RunMode.parse(mode) if mode else None
        self._require_agentic_model()
        repository_root = self._resolve_repository_root(repository, repository_root, tenant_id)
        self._validate_enabled_agents(enabled_agents)
        self._validate_enabled_skills(enabled_skills, tenant_id)
        self._authorize_repository(tenant_id, repository)
        task_id = self._create_task(
            repository, diff, pull_request, source, tenant_id,
            repository_root, enabled_agents, enabled_skills,
        )
        try:
            with self.observability.span(
                "review", task_id, task_id=task_id, tenant_id=tenant_id,
                repository=repository,
            ), metrics.timer("review_duration"):
                report = self._run_review(
                    task_id, repository, pull_request, diff, tenant_id
                )
            metrics.inc("reviews_total")
            lane = (self.store.get(task_id, tenant_id).get("input") or {}).get(
                "release_lane", "stable"
            )
            self.releases.observe(tenant_id, "llm-review", False, lane)
            return {"task_id": task_id, "state": "SUCCESS", "report": report.to_dict()}
        except Exception:
            task = self.store.get(task_id, tenant_id) or {}
            lane = (task.get("input") or {}).get("release_lane", "stable")
            self.releases.observe(tenant_id, "llm-review", True, lane)
            self.alerts.evaluate(tenant_id)
            raise

    def enqueue_review(
        self, repository: str, diff: str, pull_request: Optional[int] = None,
        source: str = "api", github_issue_url: str = "", installation_id: Optional[int] = None,
        tenant_id: str = "default", mode: str = "", repository_root: str = "",
        enabled_agents: Optional[list] = None,
        enabled_skills: Optional[list] = None,
    ) -> Dict[str, Any]:
        self._validate_review(repository, diff)
        RunMode.parse(mode) if mode else None
        self._require_agentic_model()
        repository_root = self._resolve_repository_root(repository, repository_root, tenant_id)
        self._validate_enabled_agents(enabled_agents)
        self._validate_enabled_skills(enabled_skills, tenant_id)
        self._authorize_repository(tenant_id, repository)
        task_id = self._create_task(
            repository, diff, pull_request, source, tenant_id,
            repository_root, enabled_agents, enabled_skills,
        )
        self.queue.submit({
            "task_id": task_id, "repository": repository, "pull_request": pull_request,
            "github_issue_url": github_issue_url, "installation_id": installation_id,
            "tenant_id": tenant_id,
        }, message_id=task_id)
        metrics.inc("reviews_enqueued_total")
        return {"task_id": task_id, "state": "PENDING", "queue": self.queue.backend}

    def _process_queued(self, payload: Dict[str, Any]) -> None:
        task_id = payload["task_id"]
        task = self.store.get(task_id)
        if not task:
            raise PermanentTaskError("task record no longer exists")
        tenant_id = task.get("tenant_id") or "default"
        if payload.get("tenant_id", tenant_id) != tenant_id or payload["repository"] != task["repository"]:
            raise PermanentTaskError("queue payload does not match the task's tenant and repository")
        diff = self.store.get_task_payload(task_id)
        if diff is None and payload.get("diff_url"):
            client = (
                self.github_client_for_installation(payload.get("installation_id"))
                if payload.get("installation_id") else self.github
            )
            client.ensure_repository_access(payload["repository"])
            diff = client.fetch_diff(payload["diff_url"])
            self._validate_review(payload["repository"], diff)
            encoded = diff.encode("utf-8")
            self.store.save_task_payload(task_id, diff)
            self.store.update_task_input(task_id, {
                "diff_pending": False, "diff_bytes": len(encoded),
                "diff_sha256": hashlib.sha256(encoded).hexdigest(),
            })
        if diff is None:
            raise PermanentTaskError("task payload no longer exists")
        try:
            with self.observability.span(
                "review.async", task_id, task_id=task_id, tenant_id=tenant_id,
            ), metrics.timer("review_duration"):
                report = self._run_review(
                    task_id, payload["repository"], payload.get("pull_request"), diff,
                    tenant_id,
                )
            metrics.inc("reviews_total")
            lane = (task.get("input") or {}).get("release_lane", "stable")
            self.releases.observe(tenant_id, "llm-review", False, lane)
            if payload.get("github_issue_url") and self.settings.auto_post_review:
                client = self.github_client_for_installation(payload.get("installation_id"))
                client.upsert_comment(
                    payload["github_issue_url"], to_markdown(report.to_dict()),
                    "<!-- evoagent-review:%s -->" % task_id,
                )
        except Exception:
            metrics.inc("reviews_failed_total")
            lane = (task.get("input") or {}).get("release_lane", "stable")
            self.releases.observe(tenant_id, "llm-review", True, lane)
            self.alerts.evaluate(tenant_id)
            raise

    def _on_dead_letter(self, payload: Dict[str, Any], error: str) -> None:
        task_id = payload.get("task_id", "")
        tenant_id = payload.get("tenant_id", "default")
        task = self.store.get(task_id, tenant_id) if task_id else None
        if task and task.get("state") not in {
            TaskState.SUCCESS.value, TaskState.FAILED.value, TaskState.CANCELLED.value,
        }:
            step = max(
                [int(item.get("step", 0)) for item in task.get("trace", [])] or [0]
            ) + 1
            self.store.fail(
                task_id, error,
                TraceEvent(
                    step, TaskState.FAILED,
                    "Task entered the dead-letter queue: %s" % error, utc_now(),
                ),
            )
        self.store.create_alert(
            tenant_id, "dlq:%s" % (task_id or "unknown"), "critical",
            "Task %s entered the dead-letter queue: %s" % (task_id, error),
        )
        metrics.inc("dead_letters_total")

    def handle_github_pull_request(
        self, payload: Dict[str, Any], delivery_id: str,
        payload_sha256: str, tenant_id: str = "",
    ) -> Dict[str, Any]:
        installation_id = (payload.get("installation") or {}).get("id")
        tenant_id = tenant_id or (
            self.store.installation_tenant(installation_id) if installation_id else None
        ) or self.settings.default_tenant_id
        action = payload.get("action")
        supported = action in {"opened", "reopened", "synchronize"}
        pull = payload.get("pull_request") or {}
        repository = (payload.get("repository") or {}).get("full_name", "")
        number, diff_url = payload.get("number"), pull.get("diff_url")
        if supported:
            self._require_agentic_model()
            if not repository or not isinstance(number, int) or not diff_url:
                raise ValueError("invalid GitHub pull_request payload")
            self._authorize_repository(tenant_id, repository)
        claim_token = uuid.uuid4().hex
        if not self.store.claim_webhook(
            delivery_id, tenant_id, "pull_request", payload_sha256, claim_token,
        ):
            existing = self.store.get_webhook(delivery_id) or {}
            return {
                "duplicate": True, "task_id": existing.get("task_id"),
                "state": "PENDING" if existing.get("task_id") else "ACCEPTED",
            }
        try:
            if not supported:
                self.store.complete_webhook(delivery_id, None, claim_token)
                return {"ignored": True, "reason": "unsupported pull_request action: %s" % action}
            # A retry after a crash reuses the same task even if the process died
            # between task creation and binding the delivery record.
            task_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "evoagent:webhook:%s:%s" % (tenant_id, delivery_id)))
            queue_payload = {
                "task_id": task_id, "repository": repository, "pull_request": number,
                "github_issue_url": pull.get("issue_url", ""),
                "installation_id": installation_id, "tenant_id": tenant_id,
                "diff_url": diff_url,
            }
            if not self.store.get(task_id, tenant_id):
                self._create_deferred_task(
                    repository, number, "github-webhook", tenant_id,
                    queue_payload, task_id=task_id,
                )
            self.store.bind_webhook_task(delivery_id, task_id, claim_token)
            self.queue.submit(queue_payload, message_id=task_id)
            self.store.complete_webhook(delivery_id, task_id, claim_token)
            metrics.inc("reviews_enqueued_total")
            return {"task_id": task_id, "state": "PENDING", "queue": self.queue.backend,
                    "will_post_to_github": self.settings.auto_post_review}
        except Exception:
            self.store.release_webhook(delivery_id, claim_token)
            raise

    def github_client_for_installation(self, installation_id: Optional[int] = None) -> GitHubClient:
        if installation_id is None:
            return self.github
        if not self.settings.github_app_id or not self.settings.github_private_key_path:
            raise ValueError("GitHub App credentials are not configured")
        token = GitHubAppAuthenticator(
            self.settings.github_app_id, self.settings.github_private_key_path
        ).installation_token(installation_id)
        return GitHubClient(token)

    def create_fix(
        self, task_id: str, installation_id: Optional[int] = None,
        tenant_id: Optional[str] = None,
    ) -> dict:
        task = self.store.get(task_id, tenant_id)
        if not task or not task.get("report"):
            raise ValueError("completed task not found")
        if task.get("pull_request") is None:
            raise ValueError("fix commits require a GitHub pull request task")
        actual_tenant = task.get("tenant_id") or tenant_id or "default"
        if not self.store.repository_allowed(actual_tenant, task["repository"], True):
            raise PermissionError("automatic repair is not enabled for this repository")
        result = self.fixer.create_fix_commits(
            self.github_client_for_installation(installation_id),
            task["repository"], task["pull_request"], task["report"],
        )
        metrics.inc("fix_runs_total")
        return result

    def record_feedback(
        self, task_id: str, category: str, finding: Optional[dict], note: str,
        tenant_id: Optional[str] = None,
    ) -> dict:
        task = self.store.get(task_id, tenant_id)
        if not task:
            raise ValueError("task not found")
        if task.get("state") != "SUCCESS" or not task.get("report"):
            raise ValueError("feedback requires a completed review task")
        if category not in {"false_positive", "missed_issue", "bad_fix", "accepted"}:
            raise ValueError("unsupported feedback category")
        self.store.record_failure_case(task_id, category, {"finding": finding, "note": note[:2000]})
        self.memory.remember_feedback(
            task.get("tenant_id") or tenant_id or "default", task["repository"],
            task_id, category, finding, note[:2000],
        )
        metrics.inc("feedback_total")
        return {"recorded": True, "category": category}

    def resume_task(self, task_id: str, tenant_id: Optional[str] = None) -> dict:
        task = self.store.get(task_id, tenant_id)
        if not task:
            raise ValueError("task not found")
        if task["state"] == "SUCCESS":
            return {"task_id": task_id, "state": "SUCCESS", "report": task["report"]}
        if task["state"] not in {"FAILED", "CANCELLED"}:
            raise ValueError("only failed or cancelled tasks can be resumed")
        diff = self.store.get_task_payload(task_id)
        if diff is None:
            raise ValueError("task payload is no longer available")
        if not self.store.prepare_resume(task_id, task.get("tenant_id", "default")):
            raise ValueError("task state changed; resume was not applied")
        try:
            self.queue.submit({
                "task_id": task_id, "repository": task["repository"],
                "pull_request": task.get("pull_request"),
                "tenant_id": task.get("tenant_id", "default"),
            }, message_id=task_id)
        except Exception as exc:
            step = max([int(item["step"]) for item in task.get("trace", [])] or [0]) + 1
            self.store.fail(task_id, str(exc), TraceEvent(
                step, TaskState.FAILED, "Resume delivery failed: %s" % exc, utc_now(),
            ))
            raise
        return {"task_id": task_id, "state": "PENDING", "resumed": True}

    def cancel_task(self, task_id: str, tenant_id: Optional[str] = None) -> bool:
        return self.store.request_cancel(task_id, tenant_id)

    def _authorize_repository(self, tenant_id: str, repository: str) -> None:
        if not self.store.repository_allowed(tenant_id, repository):
            raise PermissionError("repository is not authorized for this tenant")

    def _resolve_repository_root(self, repository: str, requested: str, tenant_id: str) -> str:
        configured = self.repository_roots.get(tenant_id, {}).get(repository, "")
        if not configured:
            if requested:
                raise PermissionError("no local checkout is authorized for this tenant and repository")
            return ""
        root = os.path.realpath(configured)
        if not os.path.isdir(root):
            raise ValueError("the authorized repository checkout is unavailable")
        if requested and os.path.normcase(os.path.realpath(requested)) != os.path.normcase(root):
            raise PermissionError("repository_root does not match the authorized checkout")
        return root

    @staticmethod
    def _validate_enabled_agents(enabled_agents: Optional[list]) -> None:
        if enabled_agents is None:
            return
        allowed = {"lead", "security", "correctness-reliability", "critic"}
        unknown = set(enabled_agents).difference(allowed)
        if unknown:
            raise ValueError("unsupported enabled Agent role(s): %s" % ", ".join(sorted(unknown)))

    def _validate_enabled_skills(
        self, enabled_skills: Optional[list], tenant_id: str,
    ) -> None:
        if enabled_skills is None:
            return
        if not all(isinstance(item, str) for item in enabled_skills):
            raise ValueError("enabled_skills must contain Agent Skill names")
        available = {skill.name for skill in self._active_agent_skills(tenant_id)}
        unknown = set(enabled_skills).difference(available)
        if unknown:
            raise ValueError(
                "unknown enabled Agent Skill(s): %s" % ", ".join(sorted(unknown))
            )
