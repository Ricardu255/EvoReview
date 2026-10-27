"""SQLite failure-case adapter; SQL is shared through the dialect-aware mixin."""
from ..shared.failures import FailureStoreMixin


class SqliteFailureStoreMixin(FailureStoreMixin):
    pass
