"""PostgreSQL persistence backend composed from capability mixins."""
from .adapters.stores.postgres.auth import PostgresAuthStoreMixin
from .adapters.stores.postgres.failures import PostgresFailureStoreMixin
from .adapters.stores.postgres.installations import PostgresInstallationStoreMixin
from .adapters.stores.postgres.memory import PostgresMemoryStoreMixin
from .adapters.stores.postgres.observability import PostgresObservabilityStoreMixin
from .adapters.stores.postgres.schema import PostgresSchemaMixin
from .adapters.stores.postgres.skills import PostgresSkillStoreMixin
from .adapters.stores.postgres.tasks import PostgresTaskStoreMixin
from .prompt_store import PromptStoreMixin
from .store_contract import StoreProtocol
from .webhook_store import WebhookStoreMixin


class PostgresTaskStore(
    PostgresSchemaMixin, PostgresTaskStoreMixin, PostgresMemoryStoreMixin,
    PostgresFailureStoreMixin, PostgresSkillStoreMixin,
    PostgresInstallationStoreMixin, PostgresAuthStoreMixin,
    PostgresObservabilityStoreMixin, PromptStoreMixin, WebhookStoreMixin,
    StoreProtocol,
):
    _postgres_sql = True

    def __init__(self, url: str):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError("PostgreSQL mode requires: pip install psycopg[binary]") from exc
        self.psycopg = psycopg
        self.dict_row = dict_row
        self.url = url
        self._init()

    def _connect(self):
        return self.psycopg.connect(self.url, row_factory=self.dict_row)


def create_store(database_url: str, sqlite_path: str):
    if database_url.startswith(("postgres://", "postgresql://")):
        return PostgresTaskStore(database_url)
    from .store import TaskStore
    return TaskStore(sqlite_path)
