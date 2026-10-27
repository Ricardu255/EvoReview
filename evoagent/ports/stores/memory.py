"""Tenant/repository-scoped agent memory."""
from typing import Any, Dict, Protocol, runtime_checkable


@runtime_checkable
class MemoryStorePort(Protocol):
    def save_agent_memory(self, memory: Dict[str, Any]) -> Dict[str, Any]:
        ...

    def list_agent_memories(
        self, tenant_id: str, repository: str, scopes: tuple, limit: int = 100,
    ) -> list:
        ...

    def delete_agent_memories(
        self, task_id: str = "", scope: str = "",
    ) -> int:
        ...

    def purge_expired_agent_memories(self) -> int:
        ...
