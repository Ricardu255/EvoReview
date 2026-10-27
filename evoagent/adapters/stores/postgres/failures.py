"""PostgreSQL failure-case adapter; SQL is shared through the dialect-aware mixin."""
from ..shared.failures import FailureStoreMixin


class PostgresFailureStoreMixin(FailureStoreMixin):
    pass
