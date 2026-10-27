"""Guard the SQLite/PostgreSQL adapter split."""
import datetime
from pathlib import Path
import unittest

from evoagent.adapters.stores.shared.rows import (
    decode_json, memory_from_row, skill_artifact_from_row,
)
from evoagent.postgres_store import PostgresTaskStore
from evoagent.store import TaskStore


ROOT = Path(__file__).resolve().parents[1]
STORES = ROOT / "evoagent" / "adapters" / "stores"


class StoreAdapterStructureTests(unittest.TestCase):
    def test_sqlite_store_is_composed_from_capability_mixins(self):
        names = {base.__name__ for base in TaskStore.__mro__}
        for expected in {
            "SqliteSchemaMixin", "SqliteTaskStoreMixin",
            "SqliteMemoryStoreMixin", "SqliteFailureStoreMixin",
            "SqliteSkillStoreMixin", "SqliteInstallationStoreMixin",
            "SqliteAuthStoreMixin", "SqliteObservabilityStoreMixin",
        }:
            self.assertIn(expected, names)

    def test_postgres_store_is_composed_from_capability_mixins(self):
        names = {base.__name__ for base in PostgresTaskStore.__mro__}
        for expected in {
            "PostgresSchemaMixin", "PostgresTaskStoreMixin",
            "PostgresMemoryStoreMixin", "PostgresFailureStoreMixin",
            "PostgresSkillStoreMixin", "PostgresInstallationStoreMixin",
            "PostgresAuthStoreMixin", "PostgresObservabilityStoreMixin",
        }:
            self.assertIn(expected, names)

    def test_store_facades_stay_thin(self):
        for name in ("store.py", "postgres_store.py"):
            source = (ROOT / "evoagent" / name).read_text(encoding="utf-8")
            self.assertLessEqual(len(source.splitlines()), 50, name)

    def test_each_backend_has_the_same_capability_modules(self):
        expected = {
            "__init__.py", "schema.py", "tasks.py", "memory.py", "failures.py",
            "skills.py", "installations.py", "auth.py", "observability.py",
        }
        for backend in ("sqlite", "postgres"):
            self.assertEqual(
                expected,
                {path.name for path in (STORES / backend).glob("*.py")},
                backend,
            )

    def test_adapters_do_not_import_application_or_agent_layers(self):
        for path in STORES.rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            for module in ("evoagent.service", "evoagent.app", "evoagent.agent"):
                self.assertNotIn(module, source, path.name)


class SharedRowTests(unittest.TestCase):
    def test_decode_json_accepts_sqlite_text_and_postgres_values(self):
        self.assertEqual({"a": 1}, decode_json('{"a": 1}'))
        self.assertEqual({"a": 1}, decode_json({"a": 1}))

    def test_memory_row_normalizes_both_backends(self):
        sqlite_row = {
            "keywords_json": '["a"]', "metadata_json": '{"k": 1}',
            "created_at": "2026-01-01T00:00:00",
        }
        value = memory_from_row(sqlite_row)
        self.assertEqual(["a"], value["keywords"])
        self.assertEqual({"k": 1}, value["metadata"])

        stamp = datetime.datetime(2026, 1, 1)
        pg_row = {
            "keywords_json": ["a"], "metadata_json": {"k": 1},
            "created_at": stamp,
        }
        value = memory_from_row(pg_row)
        self.assertEqual(["a"], value["keywords"])
        self.assertEqual(stamp.isoformat(), value["created_at"])

    def test_skill_artifact_row_normalizes_both_backends(self):
        sqlite_row = {"artifact_json": '{"name": "a"}', "active": 1}
        value = skill_artifact_from_row(sqlite_row)
        self.assertEqual({"name": "a"}, value["artifact"])
        self.assertTrue(value["active"])

        pg_row = {"artifact_json": {"name": "a"}, "active": True}
        value = skill_artifact_from_row(pg_row)
        self.assertEqual({"name": "a"}, value["artifact"])
        self.assertTrue(value["active"])


if __name__ == "__main__":
    unittest.main()
