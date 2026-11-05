"""PostgreSQL persistence backend composed from capability mixins."""
from ....ports.stores import StoreProtocol
from ..shared.migrations import MigrationStoreMixin
from ..shared.prompt import PromptStoreMixin
from ..shared.setup_state import SetupStateStoreMixin
from ..shared.webhook import WebhookStoreMixin
from .auth import PostgresAuthStoreMixin
from .failures import PostgresFailureStoreMixin
from .github_delivery import PostgresGitHubDeliveryStoreMixin
from .installations import PostgresInstallationStoreMixin
from .memory import PostgresMemoryStoreMixin
from .observability import PostgresObservabilityStoreMixin
from .quotas import PostgresQuotaStoreMixin
from .retention import PostgresRetentionStoreMixin
from .schema import PostgresSchemaMixin
from .skills import PostgresSkillStoreMixin
from .tasks import PostgresTaskStoreMixin


class PostgresTaskStore(
    PostgresSchemaMixin, PostgresTaskStoreMixin, PostgresMemoryStoreMixin,
    PostgresFailureStoreMixin, PostgresGitHubDeliveryStoreMixin,
    PostgresSkillStoreMixin,
    PostgresInstallationStoreMixin, PostgresAuthStoreMixin,
    PostgresObservabilityStoreMixin, PostgresQuotaStoreMixin,
    PostgresRetentionStoreMixin, PromptStoreMixin, WebhookStoreMixin,
    SetupStateStoreMixin,
    MigrationStoreMixin,
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
    from ..sqlite.store import TaskStore
    return TaskStore(sqlite_path)
