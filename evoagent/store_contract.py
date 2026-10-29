"""Compatibility exports for the persistence ports.

The contract now lives in ``evoagent.ports.stores`` and is split by business
capability.  This module keeps the historical import paths stable for one
compatibility release.
"""
import warnings

warnings.warn(
    "This top-level module is a compatibility wrapper; import from evoagent.ports.stores instead.",
    DeprecationWarning,
    stacklevel=2,
)

from .ports.stores import (
    AuthStorePort,
    EvolutionStorePort,
    FailureStorePort,
    MemoryStorePort,
    ObservabilityStorePort,
    ReviewStore,
    SkillStorePort,
    StoreProtocol,
    TaskStorePort,
    WebhookStorePort,
)

__all__ = [
    "AuthStorePort",
    "EvolutionStorePort",
    "FailureStorePort",
    "MemoryStorePort",
    "ObservabilityStorePort",
    "ReviewStore",
    "SkillStorePort",
    "StoreProtocol",
    "TaskStorePort",
    "WebhookStorePort",
]
