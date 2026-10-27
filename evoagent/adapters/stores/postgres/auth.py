"""PostgreSQL auth adapter; SQL is shared through the dialect-aware mixin."""
from ..shared.auth import AuthStoreMixin


class PostgresAuthStoreMixin(AuthStoreMixin):
    pass
