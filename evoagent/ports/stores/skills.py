"""Prompt/Skill version history and activation."""
from typing import Any, Dict, Optional, Protocol, runtime_checkable


@runtime_checkable
class SkillStorePort(Protocol):
    def save_skill_version(
        self, skill_name: str, prompt: str, score: float,
        activate: bool = False, tenant_id: str = "default",
    ) -> Dict[str, Any]:
        ...

    def get_active_skill_version(
        self, skill_name: str, tenant_id: str = "default",
    ) -> Optional[Dict[str, Any]]:
        ...

    def get_skill_version(
        self, skill_name: str, version: int, tenant_id: str = "default",
    ) -> Optional[Dict[str, Any]]:
        ...

    def list_skill_versions(
        self, skill_name: str, tenant_id: str = "default",
    ) -> list:
        ...

    def activate_skill_version(
        self, skill_name: str, version: int, tenant_id: str = "default",
    ) -> bool:
        ...

    def save_skill_artifact(
        self, skill_name: str, artifact: Dict[str, Any], score: float,
        activate: bool = False, tenant_id: str = "default",
    ) -> Dict[str, Any]:
        ...

    def get_active_skill_artifact(
        self, skill_name: str, tenant_id: str = "default",
    ) -> Optional[Dict[str, Any]]:
        ...

    def list_active_skill_artifacts(
        self, tenant_id: str = "default",
    ) -> list:
        ...

    def list_skill_artifact_versions(
        self, skill_name: str, tenant_id: str = "default",
    ) -> list:
        ...

    def activate_skill_artifact(
        self, skill_name: str, version: int, tenant_id: str = "default",
    ) -> bool:
        ...

    def save_skill_evolution_run(self, run: Dict[str, Any]) -> Dict[str, Any]:
        ...

    def list_skill_evolution_runs(
        self, limit: int = 50, tenant_id: Optional[str] = None,
    ) -> list:
        ...
