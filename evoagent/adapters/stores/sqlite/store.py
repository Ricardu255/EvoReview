"""SQLite persistence backend composed from capability mixins."""
import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator

from ....ports.stores import StoreProtocol
from ..shared.migrations import MigrationStoreMixin
from ..shared.prompt import PromptStoreMixin
from ..shared.webhook import WebhookStoreMixin
from .auth import SqliteAuthStoreMixin
from .failures import SqliteFailureStoreMixin
from .installations import SqliteInstallationStoreMixin
from .memory import SqliteMemoryStoreMixin
from .observability import SqliteObservabilityStoreMixin
from .schema import SqliteSchemaMixin
from .skills import SqliteSkillStoreMixin
from .tasks import SqliteTaskStoreMixin

__all__ = ["TaskStore"]


class TaskStore(
    SqliteSchemaMixin, SqliteTaskStoreMixin, SqliteMemoryStoreMixin,
    SqliteFailureStoreMixin, SqliteSkillStoreMixin,
    SqliteInstallationStoreMixin, SqliteAuthStoreMixin,
    SqliteObservabilityStoreMixin, PromptStoreMixin, WebhookStoreMixin,
    MigrationStoreMixin,
    StoreProtocol,
):
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._init()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        # SQLite disables foreign keys per connection by default; the schema
        # relies on them to keep child rows tied to existing tasks/users.
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        try:
            with conn:
                yield conn
        finally:
            conn.close()
