"""SQLite persistence backend composed from capability mixins."""
import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator

from .adapters.stores.sqlite.auth import SqliteAuthStoreMixin
from .adapters.stores.sqlite.failures import SqliteFailureStoreMixin
from .adapters.stores.sqlite.installations import SqliteInstallationStoreMixin
from .adapters.stores.sqlite.memory import SqliteMemoryStoreMixin
from .adapters.stores.sqlite.observability import SqliteObservabilityStoreMixin
from .adapters.stores.sqlite.schema import SqliteSchemaMixin
from .adapters.stores.sqlite.skills import SqliteSkillStoreMixin
from .adapters.stores.sqlite.tasks import SqliteTaskStoreMixin
from .prompt_store import PromptStoreMixin
from .store_contract import StoreProtocol
from .time_utils import utc_now
from .webhook_store import WebhookStoreMixin

__all__ = ["TaskStore", "utc_now"]


class TaskStore(
    SqliteSchemaMixin, SqliteTaskStoreMixin, SqliteMemoryStoreMixin,
    SqliteFailureStoreMixin, SqliteSkillStoreMixin,
    SqliteInstallationStoreMixin, SqliteAuthStoreMixin,
    SqliteObservabilityStoreMixin, PromptStoreMixin, WebhookStoreMixin,
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
        try:
            with conn:
                yield conn
        finally:
            conn.close()
