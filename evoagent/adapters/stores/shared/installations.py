"""GitHub installation persistence shared by both SQL backends."""
from typing import Optional

from ....time_utils import utc_now


class InstallationStoreMixin:
    def save_installation(
        self, installation_id: int, account_login: str,
        tenant_id: str = "default",
    ) -> None:
        with self._write() as conn:
            conn.execute(self._sql(
                "INSERT INTO installations(installation_id,account_login,created_at,tenant_id) "
                "VALUES (?,?,?,?) ON CONFLICT(installation_id) DO UPDATE SET "
                "account_login=excluded.account_login,created_at=excluded.created_at,"
                "tenant_id=excluded.tenant_id"
            ), (installation_id, account_login, utc_now(), tenant_id))

    def installation_tenant(self, installation_id: int) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute(self._sql(
                "SELECT tenant_id FROM installations WHERE installation_id=?"
            ), (installation_id,)).fetchone()
        return str(row["tenant_id"]) if row else None
