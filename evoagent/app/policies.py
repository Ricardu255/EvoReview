"""Review-facing policy checks shared by the application services."""
import os
from typing import Callable, Dict, Optional

from ..config import Settings
from ..ports.stores import StoreProtocol


class ReviewPolicy:
    def __init__(
        self,
        settings: Settings,
        store: StoreProtocol,
        repository_roots: Dict[str, Dict[str, str]],
        active_skills: Callable[[str], list],
    ):
        self.settings = settings
        self.store = store
        self.repository_roots = repository_roots
        self.active_skills = active_skills

    def validate_review(self, repository: str, diff: str) -> None:
        if not repository or len(repository) > 250:
            raise ValueError("repository is required and must be at most 250 characters")
        size = len(diff.encode("utf-8"))
        if size == 0:
            raise ValueError("diff is required")
        if size > self.settings.max_diff_bytes:
            raise ValueError(
                "diff exceeds maximum size of %d bytes" % self.settings.max_diff_bytes
            )

    @staticmethod
    def require_agentic_model(chat_client) -> None:
        if chat_client is None:
            raise RuntimeError("agentic review requires a configured model")

    def authorize_repository(self, tenant_id: str, repository: str) -> None:
        if self.settings.require_repository_grants and not self.store.has_repository_grants(tenant_id):
            raise PermissionError(
                "repository grants are not configured for this tenant"
            )
        if not self.store.repository_allowed(tenant_id, repository):
            raise PermissionError("repository is not authorized for this tenant")

    def resolve_repository_root(
        self, repository: str, requested: str, tenant_id: str,
    ) -> str:
        configured = self.repository_roots.get(tenant_id, {}).get(repository, "")
        if not configured:
            if requested:
                raise PermissionError(
                    "no local checkout is authorized for this tenant and repository"
                )
            return ""
        root = os.path.realpath(configured)
        if not os.path.isdir(root):
            raise ValueError("the authorized repository checkout is unavailable")
        if requested and os.path.normcase(os.path.realpath(requested)) != os.path.normcase(root):
            raise PermissionError("repository_root does not match the authorized checkout")
        return root

    @staticmethod
    def validate_enabled_agents(enabled_agents: Optional[list]) -> None:
        if enabled_agents is None:
            return
        allowed = {"lead", "security", "correctness-reliability", "critic"}
        unknown = set(enabled_agents).difference(allowed)
        if unknown:
            raise ValueError(
                "unsupported enabled Agent role(s): %s" % ", ".join(sorted(unknown))
            )

    def validate_enabled_skills(
        self, enabled_skills: Optional[list], tenant_id: str,
    ) -> None:
        if enabled_skills is None:
            return
        if not all(isinstance(item, str) for item in enabled_skills):
            raise ValueError("enabled_skills must contain Agent Skill names")
        available = {skill.name for skill in self.active_skills(tenant_id)}
        unknown = set(enabled_skills).difference(available)
        if unknown:
            raise ValueError(
                "unknown enabled Agent Skill(s): %s" % ", ".join(sorted(unknown))
            )
