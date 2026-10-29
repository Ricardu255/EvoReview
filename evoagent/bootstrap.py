"""Composition root for the review service.

The service object owns behaviour; this module owns wiring.  Keeping every
concrete dependency construction here makes it possible to replace stores,
queues and model clients without editing ``ReviewService``.
"""
from dataclasses import dataclass
import json
import os
import threading
from typing import Any, Callable, Dict, Optional

from .agent.reviewer import AgenticReviewer
from .adapters.auth import AuthManager
from .config import Settings
from .agent.context_manager import ContextManager
from .evaluation.evolution import EvolutionEngine
from .evaluation.evolution_v2 import RootCauseEvolutionGenerator
from .adapters.github import GitHubClient
from .agent.harness import ReviewHarness
from .adapters.llm import JsonChatClient
from .agent.memory import MemoryManager
from .adapters.observability import AlertManager, Observability
from .adapters.patching import SuggestionOnlyFixer, VerifiedPatchFixer
from .adapters.stores.postgres.store import create_store
from .agent.reviewers import (
    OpenAICompatibleReviewer, ReliabilityRuleReviewer, SecurityRuleReviewer,
)
from .adapters.rollout import ReleaseManager
from .evaluation.skill_evolution import AgentSkillReplayReviewer, SkillEvolutionEngine, validate_artifact
from .evaluation.skills import AgentSkill, SkillRegistry
from .ports.stores import StoreProtocol
from .adapters.task_queue import TaskQueue
from .adapters.verifier import RepairVerifier


@dataclass
class ServiceContainer:
    """Fully constructed service dependencies; no behaviour lives here."""

    settings: Settings
    repository_roots: Dict[str, Dict[str, str]]
    reload_lock: Any
    llm_config: Optional[Dict[str, Any]]
    store: StoreProtocol
    memory: MemoryManager
    context_manager: ContextManager
    observability: Observability
    registry: SkillRegistry
    chat_client: Any
    reviewer: AgenticReviewer
    harness: ReviewHarness
    github: GitHubClient
    fixer: Any
    auth: AuthManager
    releases: ReleaseManager
    alerts: AlertManager
    evolution: EvolutionEngine
    skill_evolution: SkillEvolutionEngine
    tenant_evolutions: Dict[str, EvolutionEngine]

    def build_queue(
        self,
        process: Callable[[Dict[str, Any]], None],
        dead_letter: Callable[[Dict[str, Any], str], None],
    ) -> TaskQueue:
        """Create the queue after the service callbacks exist."""
        settings = self.settings
        return TaskQueue(
            process,
            settings.async_workers,
            settings.redis_url,
            settings.queue_max_attempts,
            settings.queue_lease_seconds,
            dead_letter,
        )


def _parse_repository_roots(settings: Settings) -> Dict[str, Dict[str, str]]:
    repository_roots = json.loads(settings.repository_roots_json)
    if not isinstance(repository_roots, dict) or any(
        not isinstance(repositories, dict)
        or any(not isinstance(path, str) or not os.path.isabs(path)
               for path in repositories.values())
        for repositories in repository_roots.values()
    ):
        raise ValueError(
            "repository roots must map tenant IDs to repository/absolute-path mappings"
        )
    return repository_roots


def build_llm_reviewer(
    settings: Settings,
    llm_config: Optional[Dict[str, Any]],
    prompt: str = "",
) -> OpenAICompatibleReviewer:
    if not llm_config:
        raise RuntimeError("no LLM provider is configured")
    return OpenAICompatibleReviewer(
        str(llm_config["base_url"]),
        str(llm_config["api_key"]),
        str(llm_config["model"]),
        settings.timeout_seconds,
        system_prompt=prompt,
        provider=str(llm_config["provider"]),
        extra_headers=dict(llm_config.get("headers") or {}),
    )


def active_agent_skills(
    store: StoreProtocol, registry: SkillRegistry, tenant_id: str,
) -> list:
    values = {skill.name: skill for skill in registry.agent_skills()}
    for version in store.list_active_skill_artifacts(tenant_id):
        artifact = validate_artifact(version["artifact"], version["skill_name"])
        values[version["skill_name"]] = AgentSkill.from_artifact(
            artifact, str(version["version"])
        )
    return [values[name] for name in sorted(values)]


def build_agentic_reviewer(
    settings: Settings,
    store: StoreProtocol,
    chat_client: Any,
    registry: SkillRegistry,
    memory: MemoryManager,
    context_manager: ContextManager,
    active_skills: Callable[[str], list],
    tenant_id: Optional[str] = None,
    prompt_version: Optional[int] = None,
    shadow: bool = False,
) -> AgenticReviewer:
    tenant_id = tenant_id or settings.default_tenant_id
    enabled = {
        item.strip() for item in settings.enabled_agents.split(",") if item.strip()
    }
    unknown = enabled.difference({
        "lead", "security", "correctness-reliability", "critic"
    })
    if unknown:
        raise ValueError(
            "unsupported enabled Agent role(s): %s" % ", ".join(sorted(unknown))
        )
    active_prompt = (
        store.get_active_skill_version("llm-review", tenant_id)
        if prompt_version is None else
        store.get_skill_version("llm-review", prompt_version, tenant_id)
        if prompt_version else None
    )
    if prompt_version and not active_prompt:
        raise ValueError("the task's pinned prompt version is unavailable")
    structured_config = {}
    if active_prompt:
        run = next((
            item for item in store.list_evolution_runs(200, tenant_id)
            if item["skill_name"] == "llm-review"
            if int(item.get("candidate_version") or 0) == int(active_prompt["version"])
            and (item.get("metrics") or {}).get("structured_candidate")
        ), None)
        if run:
            structured_config = (
                run["metrics"]["structured_candidate"].get("candidate") or {}
            )
    return AgenticReviewer(
        store, chat_client,
        settings.agent_token_budget,
        settings.agent_time_budget_seconds,
        settings.llm_input_cost_per_million,
        settings.llm_output_cost_per_million,
        enabled,
        [
            item for item in registry.reviewers()
            if not isinstance(item, OpenAICompatibleReviewer)
        ],
        None,
        settings.repair_test_command,
        active_prompt["prompt"] if active_prompt else "",
        structured_config,
        None if shadow else memory,
        context_manager,
        active_skills,
    )


def build_container(settings: Settings) -> ServiceContainer:
    """Build every dependency once, outside the service object."""
    settings.validate_evolution()
    repository_roots = _parse_repository_roots(settings)
    llm_config = settings.resolved_llm()
    store = create_store(settings.database_url, settings.db_path)
    store.migrate_legacy_tenant(settings.default_tenant_id)
    memory = MemoryManager(
        store, settings.memory_enabled, settings.memory_recall_limit,
        settings.memory_working_ttl_seconds,
    )
    context_manager = ContextManager(
        settings.agent_context_window_tokens,
        settings.agent_context_input_tokens,
        settings.context_diff_token_budget,
        settings.context_observation_token_budget,
        settings.context_recent_observations,
        settings.context_map_chunk_tokens,
    )
    observability = Observability(settings.otel_service_name, settings.otel_endpoint)
    registry = SkillRegistry(settings.skills_dir)
    registry.register(
        "security-review", SecurityRuleReviewer(),
        "1.0.0", "Security, injection and secret detection",
    )
    registry.register(
        "reliability-review", ReliabilityRuleReviewer(),
        "1.0.0", "Reliability and observability review",
    )
    if llm_config:
        active = store.get_active_skill_version(
            "llm-review", settings.default_tenant_id
        )
        registry.register(
            "llm-review",
            build_llm_reviewer(settings, llm_config, active["prompt"] if active else ""),
            "1.0.0",
            "Context-aware AI code review via %s" % llm_config["provider"],
        )
    registry.reload()
    chat_client = (
        JsonChatClient(
            str(llm_config["base_url"]), str(llm_config["api_key"]),
            str(llm_config["model"]), str(llm_config["provider"]),
            settings.timeout_seconds, dict(llm_config.get("headers") or {}),
            retries=settings.llm_max_retries,
            backoff_seconds=settings.llm_retry_backoff_seconds,
        ) if llm_config else None
    )
    active_skills = lambda tenant_id: active_agent_skills(store, registry, tenant_id)
    reviewer = build_agentic_reviewer(
        settings, store, chat_client, registry, memory, context_manager, active_skills,
    )
    harness = ReviewHarness(
        store, reviewer, settings.max_steps, settings.timeout_seconds,
        observability=observability,
    )
    github = GitHubClient(settings.github_token)
    repair_verifier = RepairVerifier(
        settings.repair_test_command, settings.repair_verify_timeout_seconds
    )
    fixer = (
        VerifiedPatchFixer(chat_client, repair_verifier)
        if chat_client else SuggestionOnlyFixer()
    )
    auth = AuthManager(
        store, settings.auth_secret, settings.session_ttl_seconds,
        settings.bootstrap_admin_username, settings.bootstrap_admin_password,
        settings.default_tenant_id,
    )
    releases = ReleaseManager(store)
    alerts = AlertManager(
        store, settings.alert_failure_rate, settings.alert_min_samples
    )
    evolution = EvolutionEngine(
        store,
        reviewer_factory=(
            lambda prompt="": build_llm_reviewer(settings, llm_config, prompt)
        ) if llm_config else None,
        min_cases=settings.eval_min_cases,
        max_cases=settings.eval_max_cases,
        min_improvement=settings.eval_min_improvement,
        min_holdout_cases=settings.eval_min_holdout_cases,
        max_metric_regression=settings.eval_max_metric_regression,
        candidate_generator=(
            RootCauseEvolutionGenerator(chat_client)
            if chat_client else None
        ),
        tenant_id=settings.default_tenant_id,
    )
    skill_evolution = SkillEvolutionEngine(
        store,
        reviewer_factory=(lambda artifact: AgentSkillReplayReviewer(
            artifact, chat_client,
            settings.agent_token_budget,
            settings.agent_time_budget_seconds,
        )) if chat_client else None,
        min_cases=settings.eval_min_cases,
        max_cases=settings.eval_max_cases,
        min_improvement=settings.eval_min_improvement,
        min_holdout_cases=settings.eval_min_holdout_cases,
        max_metric_regression=settings.eval_max_metric_regression,
    )
    return ServiceContainer(
        settings=settings,
        repository_roots=repository_roots,
        reload_lock=threading.Lock(),
        llm_config=llm_config,
        store=store,
        memory=memory,
        context_manager=context_manager,
        observability=observability,
        registry=registry,
        chat_client=chat_client,
        reviewer=reviewer,
        harness=harness,
        github=github,
        fixer=fixer,
        auth=auth,
        releases=releases,
        alerts=alerts,
        evolution=evolution,
        skill_evolution=skill_evolution,
        tenant_evolutions={settings.default_tenant_id: evolution},
    )
