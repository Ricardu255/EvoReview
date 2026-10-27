"""Tenant-scoped prompt evolution engines."""
from typing import Any, Dict


class EvolutionApplicationService:
    def __init__(
        self, evolution: Any, reload_lock: Any,
        tenant_evolutions: Dict[str, Any],
    ):
        self.evolution = evolution
        self.reload_lock = reload_lock
        self.tenant_evolutions = tenant_evolutions

    def evolution_for(self, tenant_id: str) -> Any:
        with self.reload_lock:
            if tenant_id not in self.tenant_evolutions:
                self.tenant_evolutions[tenant_id] = self.evolution.for_tenant(tenant_id)
            return self.tenant_evolutions[tenant_id]
