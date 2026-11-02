"""One-time GitHub App setup states shared by both SQL backends."""
import time
from typing import Optional

from ....time_utils import utc_now


class SetupStateStoreMixin:
    """Persist short-lived, tenant-bound setup tokens.

    Only the token hash is stored.  Consumption is atomic: a token can be used
    exactly once, and expiry is checked in the same DELETE that removes it.
    """

    def save_github_setup_state(
        self, token_hash: str, tenant_id: str, expires_at: float,
    ) -> None:
        with self._write() as conn:
            conn.execute(self._sql(
                "DELETE FROM github_setup_states WHERE expires_at<=?"
            ), (time.time(),))
            conn.execute(self._sql(
                "INSERT INTO github_setup_states"
                "(token_hash,tenant_id,expires_at,created_at) VALUES (?,?,?,?)"
            ), (token_hash, tenant_id, float(expires_at), utc_now()))

    def consume_github_setup_state(self, token_hash: str) -> Optional[str]:
        now = time.time()
        with self._write() as conn:
            row = conn.execute(self._sql(
                "SELECT tenant_id FROM github_setup_states WHERE token_hash=?"
            ), (token_hash,)).fetchone()
            if row is None:
                return None
            cursor = conn.execute(self._sql(
                "DELETE FROM github_setup_states "
                "WHERE token_hash=? AND expires_at>?"
            ), (token_hash, now))
            if not cursor.rowcount:
                return None
            return str(row["tenant_id"])
