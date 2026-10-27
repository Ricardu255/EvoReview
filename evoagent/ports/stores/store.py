"""Aggregate persistence protocol kept for backwards compatibility."""
from typing import Protocol, runtime_checkable

from .auth import AuthStorePort
from .evolution import EvolutionStorePort
from .failures import FailureStorePort
from .memory import MemoryStorePort
from .observability import ObservabilityStorePort
from .skills import SkillStorePort
from .tasks import TaskStorePort
from .webhooks import WebhookStorePort


@runtime_checkable
class StoreProtocol(
    TaskStorePort,
    MemoryStorePort,
    FailureStorePort,
    EvolutionStorePort,
    SkillStorePort,
    WebhookStorePort,
    AuthStorePort,
    ObservabilityStorePort,
    Protocol,
):
    """The original 65-method contract, now composed from narrow ports."""
