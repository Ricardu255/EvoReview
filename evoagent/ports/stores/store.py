"""Aggregate persistence protocol kept for backwards compatibility."""
from typing import Protocol, runtime_checkable

from .auth import AuthStorePort
from .evolution import EvolutionStorePort
from .failures import FailureStorePort
from .github_delivery import GitHubDeliveryStorePort
from .memory import MemoryStorePort
from .observability import ObservabilityStorePort
from .quotas import QuotaStorePort
from .setup import SetupStateStorePort
from .skills import SkillStorePort
from .tasks import TaskStorePort
from .webhooks import WebhookStorePort


@runtime_checkable
class StoreProtocol(
    TaskStorePort,
    MemoryStorePort,
    FailureStorePort,
    GitHubDeliveryStorePort,
    EvolutionStorePort,
    SkillStorePort,
    WebhookStorePort,
    AuthStorePort,
    ObservabilityStorePort,
    QuotaStorePort,
    SetupStateStorePort,
    Protocol,
):
    """The aggregate persistence contract composed from narrow ports."""

    def migrate_legacy_tenant(self, default_tenant_id: str) -> bool:
        ...

    def health_check(self) -> bool:
        ...
