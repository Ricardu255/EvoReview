"""Compatibility facade for the review application services."""
from typing import Any, Dict, Optional

from .agent.reviewer import AgenticReviewer
from .app.evolution import EvolutionApplicationService
from .app.feedback import FeedbackApplicationService
from .app.fixes import FixesApplicationService
from .app.github import GitHubApplicationService
from .app.policies import ReviewPolicy
from .app.reviews import ReviewsApplicationService
from .app.skills import SkillApplicationService
from .bootstrap import (
    active_agent_skills, build_agentic_reviewer, build_container,
    build_llm_reviewer,
)
from .config import Settings
from .evaluation.evolution import EvolutionEngine
from .adapters.github import GitHubAppAuthenticator, GitHubClient
from .agent.harness import ReviewHarness
from .adapters.metrics import metrics
from .agent.reviewers import OpenAICompatibleReviewer


class ReviewService:
    def __init__(self, settings: Settings):
        container = build_container(settings)
        self.settings = container.settings
        self.repository_roots = container.repository_roots
        self._reload_lock = container.reload_lock
        self.llm_config = container.llm_config
        self.store = container.store
        self.memory = container.memory
        self.context_manager = container.context_manager
        self.observability = container.observability
        self.registry = container.registry
        self.chat_client = container.chat_client
        self.reviewer = container.reviewer
        self.harness = container.harness
        self.github = container.github
        self.fixer = container.fixer
        self.auth = container.auth
        self.releases = container.releases
        self.alerts = container.alerts
        self.evolution = container.evolution
        self.skill_evolution = container.skill_evolution
        self._tenant_evolutions = container.tenant_evolutions

        self._policy = ReviewPolicy(
            self.settings, self.store, self.repository_roots,
            self._active_agent_skills,
        )
        self._feedback_app = FeedbackApplicationService(
            self.store, self.memory, metrics,
        )
        self._evolution_app = EvolutionApplicationService(
            self.evolution, self._reload_lock, self._tenant_evolutions,
        )
        self._skills_app = SkillApplicationService(
            self.settings, self.store, self.registry, self.llm_config,
            self._reload_lock,
            lambda prompt: build_llm_reviewer(
                self.settings, self.llm_config, prompt,
            ),
            self._build_agentic_reviewer,
            self._active_agent_skills,
            lambda reviewer: ReviewHarness(
                self.store, reviewer, self.settings.max_steps,
                self.settings.timeout_seconds,
                observability=self.observability,
            ),
        )

        # The queue starts empty, so its callbacks can be bound before the
        # review service exists. They only resolve it when a job is delivered.
        self.queue = container.build_queue(
            self._process_queued, self._on_dead_letter
        )
        self._reviews_app = ReviewsApplicationService(
            self.settings, self.store, self.queue, self._policy,
            self.observability, self.releases, self.alerts, metrics,
            self._build_agentic_reviewer,
            lambda reviewer: ReviewHarness(
                self.store, reviewer, self.settings.max_steps,
                self.settings.timeout_seconds,
                observability=self.observability,
            ),
            lambda installation_id: self._github_app.github_client_for_installation(
                installation_id
            ),
            lambda: self.chat_client,
        )
        self._github_app = GitHubApplicationService(
            self.settings, self.store, self.queue, self._policy,
            lambda: self.chat_client, self.github, self._make_installation_client,
            self._reviews_app.create_deferred_task, metrics,
        )
        self._fixes_app = FixesApplicationService(
            self.store, self.fixer,
            self._github_app.github_client_for_installation, metrics,
            self._policy,
        )

    @property
    def repository_roots(self):
        return self._repository_roots

    @repository_roots.setter
    def repository_roots(self, value):
        self._repository_roots = value
        if hasattr(self, "_policy"):
            self._policy.repository_roots = value

    def _build_llm_reviewer(self, prompt: str = "") -> OpenAICompatibleReviewer:
        return build_llm_reviewer(self.settings, self.llm_config, prompt)

    def evolution_for(self, tenant_id: str) -> EvolutionEngine:
        return self._evolution_app.evolution_for(tenant_id)

    def _build_agentic_reviewer(
        self, tenant_id: Optional[str] = None, prompt_version: Optional[int] = None,
        shadow: bool = False,
    ) -> AgenticReviewer:
        return build_agentic_reviewer(
            self.settings, self.store, self.chat_client, self.registry,
            self.memory, self.context_manager, self._active_agent_skills,
            tenant_id, prompt_version, shadow,
        )

    def reload_skills(self) -> list:
        reloaded = self._skills_app.reload()
        self.reviewer = reloaded.reviewer
        self.harness = reloaded.harness
        return reloaded.skills

    def _active_agent_skills(self, tenant_id: str) -> list:
        return active_agent_skills(self.store, self.registry, tenant_id)

    def list_skills(self, tenant_id: str) -> list:
        return self._skills_app.list_skills(tenant_id)

    def _validate_review(self, repository: str, diff: str) -> None:
        self._policy.validate_review(repository, diff)

    def _require_agentic_model(self) -> None:
        self._policy.require_agentic_model(self.chat_client)

    def _authorize_repository(self, tenant_id: str, repository: str) -> None:
        self._policy.authorize_repository(tenant_id, repository)

    def _resolve_repository_root(
        self, repository: str, requested: str, tenant_id: str,
    ) -> str:
        return self._policy.resolve_repository_root(repository, requested, tenant_id)

    @staticmethod
    def _validate_enabled_agents(enabled_agents: Optional[list]) -> None:
        ReviewPolicy.validate_enabled_agents(enabled_agents)

    def _validate_enabled_skills(
        self, enabled_skills: Optional[list], tenant_id: str,
    ) -> None:
        self._policy.validate_enabled_skills(enabled_skills, tenant_id)

    def _run_review(
        self, task_id: str, repository: str, pull_request: Optional[int],
        diff: str, tenant_id: str,
    ):
        return self._reviews_app.run_review(
            task_id, repository, pull_request, diff, tenant_id,
        )

    def _execution_assignment(self, tenant_id: str, task_id: str) -> dict:
        return self._reviews_app._execution_assignment(tenant_id, task_id)

    def _release_result(self, report) -> dict:
        return self._reviews_app._release_result(report)

    def _run_shadow(
        self, task_id, repository, pull_request, diff, tenant_id,
        task_input, primary,
    ):
        return self._reviews_app._run_shadow(
            task_id, repository, pull_request, diff, tenant_id,
            task_input, primary,
        )

    def _create_task(
        self, repository: str, diff: str, pull_request: Optional[int], source: str,
        tenant_id: str = "default", repository_root: str = "",
        enabled_agents: Optional[list] = None,
        enabled_skills: Optional[list] = None,
    ) -> str:
        return self._reviews_app.create_task(
            repository, diff, pull_request, source, tenant_id,
            repository_root, enabled_agents, enabled_skills,
        )

    def _create_deferred_task(
        self, repository: str, pull_request: Optional[int], source: str,
        tenant_id: str, payload: Dict[str, Any], task_id: Optional[str] = None,
    ) -> str:
        return self._reviews_app.create_deferred_task(
            repository, pull_request, source, tenant_id, payload, task_id,
        )

    def create_review(
        self, repository: str, diff: str, pull_request: Optional[int] = None,
        source: str = "api", tenant_id: str = "default", mode: str = "",
        repository_root: str = "", enabled_agents: Optional[list] = None,
        enabled_skills: Optional[list] = None,
    ) -> Dict[str, Any]:
        return self._reviews_app.create_review(
            repository, diff, pull_request, source, tenant_id, mode,
            repository_root, enabled_agents, enabled_skills,
        )

    def enqueue_review(
        self, repository: str, diff: str, pull_request: Optional[int] = None,
        source: str = "api", github_issue_url: str = "",
        installation_id: Optional[int] = None,
        tenant_id: str = "default", mode: str = "", repository_root: str = "",
        enabled_agents: Optional[list] = None,
        enabled_skills: Optional[list] = None,
    ) -> Dict[str, Any]:
        return self._reviews_app.enqueue_review(
            repository, diff, pull_request, source, github_issue_url,
            installation_id, tenant_id, mode, repository_root,
            enabled_agents, enabled_skills,
        )

    def _process_queued(self, payload: Dict[str, Any]) -> None:
        return self._reviews_app.process_queued(payload)

    def _on_dead_letter(self, payload: Dict[str, Any], error: str) -> None:
        if hasattr(self, "_reviews_app"):
            return self._reviews_app.on_dead_letter(payload, error)
        # Compatibility for callers that construct the facade with only a store.
        from .app.reviews import mark_dead_letter
        return mark_dead_letter(self.store, metrics, payload, error)

    def handle_github_pull_request(
        self, payload: Dict[str, Any], delivery_id: str,
        payload_sha256: str, tenant_id: str = "",
    ) -> Dict[str, Any]:
        return self._github_app.handle_github_pull_request(
            payload, delivery_id, payload_sha256, tenant_id,
        )

    def _make_installation_client(self, installation_id: int) -> GitHubClient:
        if not self.settings.github_app_id or not self.settings.github_private_key_path:
            raise ValueError("GitHub App credentials are not configured")
        token = GitHubAppAuthenticator(
            self.settings.github_app_id, self.settings.github_private_key_path
        ).installation_token(installation_id)
        return GitHubClient(token)

    def github_client_for_installation(
        self, installation_id: Optional[int] = None,
    ) -> GitHubClient:
        return self._github_app.github_client_for_installation(installation_id)

    def create_fix(
        self, task_id: str, installation_id: Optional[int] = None,
        tenant_id: Optional[str] = None,
    ) -> dict:
        return self._fixes_app.create_fix(task_id, installation_id, tenant_id)

    def record_feedback(
        self, task_id: str, category: str, finding: Optional[dict], note: str,
        tenant_id: Optional[str] = None,
    ) -> dict:
        return self._feedback_app.record_feedback(
            task_id, category, finding, note, tenant_id,
        )

    def resume_task(self, task_id: str, tenant_id: Optional[str] = None) -> dict:
        return self._reviews_app.resume_task(task_id, tenant_id)

    def redeliver_comment(
        self, task_id: str, tenant_id: Optional[str] = None,
    ) -> dict:
        return self._reviews_app.redeliver_comment(task_id, tenant_id)

    def cancel_task(self, task_id: str, tenant_id: Optional[str] = None) -> bool:
        return self._reviews_app.cancel_task(task_id, tenant_id)
