"""SQLite persistence backend composed from capability mixins."""
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from typing import Iterator

from ....ports.stores import StoreProtocol
from ..shared.migrations import MigrationStoreMixin
from ..shared.prompt import PromptStoreMixin
from ..shared.setup_state import SetupStateStoreMixin
from ..shared.webhook import WebhookStoreMixin
from .auth import SqliteAuthStoreMixin
from .failures import SqliteFailureStoreMixin
from .github_delivery import SqliteGitHubDeliveryStoreMixin
from .installations import SqliteInstallationStoreMixin
from .memory import SqliteMemoryStoreMixin
from .observability import SqliteObservabilityStoreMixin
from .quotas import SqliteQuotaStoreMixin
from .retention import SqliteRetentionStoreMixin
from .schema import SqliteSchemaMixin
from .skills import SqliteSkillStoreMixin
from .tasks import SqliteTaskStoreMixin

__all__ = ["TaskStore"]


class TaskStore(
    SqliteSchemaMixin, SqliteTaskStoreMixin, SqliteMemoryStoreMixin,
    SqliteFailureStoreMixin, SqliteGitHubDeliveryStoreMixin,
    SqliteSkillStoreMixin,
    SqliteInstallationStoreMixin, SqliteAuthStoreMixin,
    SqliteObservabilityStoreMixin, SqliteQuotaStoreMixin,
    SqliteRetentionStoreMixin, PromptStoreMixin, WebhookStoreMixin,
    SetupStateStoreMixin,
    MigrationStoreMixin,
    StoreProtocol,
):
    def __init__(self, path: str):
        self._memory = path == ":memory:"
        # A plain ":memory:" database is private to each connection, so the
        # schema would disappear as soon as _init() closed its connection.
        # A shared-cache URI gives every connection the same database, and the
        # anchor connection keeps it alive for the store's lifetime.
        self.path = (
            "file:evoagent-memory-%s?mode=memory&cache=shared" % uuid.uuid4().hex
            if self._memory else path
        )
        self._lock = threading.Lock()
        self._anchor = (
            sqlite3.connect(self.path, uri=True, check_same_thread=False)
            if self._memory else None
        )
        self._init()

    def close(self) -> None:
        anchor, self._anchor = self._anchor, None
        if anchor is not None:
            anchor.close()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10, uri=self._memory)
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
