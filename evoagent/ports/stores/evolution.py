"""Prompt and Skill evolution run history."""
from typing import Any, Dict, Protocol, runtime_checkable


@runtime_checkable
class EvolutionStorePort(Protocol):
    def save_evolution_run(self, run: Dict[str, Any]) -> Dict[str, Any]:
        ...

    def list_evolution_runs(self, limit: int = 50, tenant_id: str = "default") -> list:
        ...

    def update_evolution_run(
        self, run_id: str, decision: str, metrics: Dict[str, Any], tenant_id: str = "default",
    ) -> bool:
        ...
