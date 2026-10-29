"""Contract tests for the split persistence ports."""
import os
import tempfile
import unittest

from evoagent.ports.stores import (
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
from evoagent.adapters.stores.sqlite.store import TaskStore


class StorePortCompositionTests(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.store = TaskStore(self.path)

    def tearDown(self):
        os.unlink(self.path)

    def test_aggregate_protocol_is_composed_from_capability_ports(self):
        for port in (
            TaskStorePort, MemoryStorePort, FailureStorePort,
            EvolutionStorePort, SkillStorePort, WebhookStorePort,
            AuthStorePort, ObservabilityStorePort,
        ):
            self.assertTrue(issubclass(StoreProtocol, port), port.__name__)

    def test_sqlite_store_satisfies_every_port(self):
        for port in (
            StoreProtocol, TaskStorePort, ReviewStore, MemoryStorePort,
            FailureStorePort, EvolutionStorePort, SkillStorePort,
            WebhookStorePort, AuthStorePort, ObservabilityStorePort,
        ):
            self.assertIsInstance(self.store, port, port.__name__)

    def test_ports_keep_the_expected_methods(self):
        expected = {
            TaskStorePort: {"create", "transition", "get", "save_checkpoint"},
            MemoryStorePort: {"save_agent_memory", "list_agent_memories"},
            FailureStorePort: {"record_failure_case", "save_evaluation_case"},
            EvolutionStorePort: {"save_evolution_run", "update_evolution_run"},
            SkillStorePort: {"save_skill_artifact", "activate_skill_version"},
            WebhookStorePort: {"claim_webhook", "get_webhook"},
            AuthStorePort: {"create_user", "has_repository_grants", "repository_allowed", "audit"},
            ObservabilityStorePort: {"dashboard_stats", "create_alert"},
        }
        for port, names in expected.items():
            for name in names:
                self.assertTrue(hasattr(port, name), "%s.%s" % (port.__name__, name))

    def test_aggregate_keeps_the_historical_method_count(self):
        # 62 methods before the split; keep the aggregate contract stable.
        # ``dir`` avoids the private Protocol internals that differ before 3.12.
        # Additions are deliberate: grant management (list/revoke) and the
        # one-time legacy-tenant migration hook.
        names = {name for name in dir(StoreProtocol) if not name.startswith("_")}
        self.assertEqual(66, len(names))


if __name__ == "__main__":
    unittest.main()
