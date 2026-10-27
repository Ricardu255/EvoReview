"""SQLite installation adapter; SQL is shared through the dialect-aware mixin."""
from ..shared.installations import InstallationStoreMixin


class SqliteInstallationStoreMixin(InstallationStoreMixin):
    pass
