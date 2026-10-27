"""Skill catalog reload and tenant-scoped active Skill listing."""
from typing import Any, Callable, NamedTuple

from ..config import Settings
from ..store_contract import StoreProtocol


class ReloadedSkills(NamedTuple):
    skills: list
    reviewer: Any
    harness: Any


class SkillApplicationService:
    def __init__(
        self,
        settings: Settings,
        store: StoreProtocol,
        registry: Any,
        llm_config: Any,
        reload_lock: Any,
        build_llm_reviewer: Callable[[str], Any],
        build_agentic_reviewer: Callable[[], Any],
        active_skills: Callable[[str], list],
        harness_factory: Callable[[Any], Any],
    ):
        self.settings = settings
        self.store = store
        self.registry = registry
        self.llm_config = llm_config
        self.reload_lock = reload_lock
        self.build_llm_reviewer = build_llm_reviewer
        self.build_agentic_reviewer = build_agentic_reviewer
        self.active_skills = active_skills
        self.harness_factory = harness_factory

    def reload(self) -> ReloadedSkills:
        with self.reload_lock:
            if self.llm_config:
                active = self.store.get_active_skill_version(
                    "llm-review", self.settings.default_tenant_id
                )
                self.registry.register(
                    "llm-review",
                    self.build_llm_reviewer(active["prompt"] if active else ""),
                    "1.0.0",
                    "Context-aware AI code review via %s" % self.llm_config["provider"],
                )
            self.registry.reload()
            skills = self.registry.list()
            reviewer = self.build_agentic_reviewer()
            harness = self.harness_factory(reviewer)
            # Swap only after both objects exist so a request thread always sees
            # a harness built around the reviewer it will actually execute.
            return ReloadedSkills(skills=skills, reviewer=reviewer, harness=harness)

    def list_skills(self, tenant_id: str) -> list:
        scanners = [
            item for item in self.registry.list() if item.get("kind") == "scanner"
        ]
        return scanners + [{
            "name": skill.name, "version": skill.version,
            "description": skill.description, "source": skill.source,
            "kind": "agent-skill", "sandboxed": False,
            "permissions": list(skill.allowed_tools),
            "content_sha256": skill.content_sha256,
            "resources": list(skill.resource_paths),
        } for skill in self.active_skills(tenant_id)]
