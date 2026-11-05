"""Tenant quota and daily usage persistence."""
from typing import Any, Dict, Optional, Protocol, runtime_checkable


@runtime_checkable
class QuotaStorePort(Protocol):
    def get_tenant_quota(self, tenant_id: str) -> Optional[Dict[str, Any]]:
        ...

    def set_tenant_quota(
        self, tenant_id: str, daily_reviews: int, daily_tokens: int,
        daily_cost_usd: float,
    ) -> Dict[str, Any]:
        ...

    def get_tenant_usage(self, tenant_id: str, day: str) -> Dict[str, Any]:
        ...

    def consume_tenant_quota(
        self, tenant_id: str, day: str, reviews: int = 0, tokens: int = 0,
        cost_usd: float = 0.0, defaults: Optional[Dict[str, Any]] = None,
        enforce: bool = True,
    ) -> bool:
        ...
