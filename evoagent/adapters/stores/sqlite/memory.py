"""SQLite memory adapter; SQL is shared through the dialect-aware mixin."""
from ..shared.memory import MemoryStoreMixin


class SqliteMemoryStoreMixin(MemoryStoreMixin):
    pass
