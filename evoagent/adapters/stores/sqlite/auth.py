"""SQLite auth adapter; SQL is shared through the dialect-aware mixin."""
from ..shared.auth import AuthStoreMixin


class SqliteAuthStoreMixin(AuthStoreMixin):
    pass
