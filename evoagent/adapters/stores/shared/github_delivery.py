"""GitHub delivery state shared by both SQL backends."""
from typing import Any, Dict, Optional

from ....time_utils import utc_now


class GitHubDeliveryStoreMixin:
    @staticmethod
    def _delivery_key(tenant_id, repository, pull_request, head_sha):
        return (str(tenant_id), str(repository), int(pull_request), str(head_sha))

    def get_github_delivery_state(
        self, tenant_id: str, repository: str, pull_request: int, head_sha: str,
    ) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(self._sql(
                "SELECT tenant_id,repository,pull_request,head_sha,"
                "inline_delivered,check_run_id,updated_at "
                "FROM github_delivery_state WHERE tenant_id=? AND repository=? "
                "AND pull_request=? AND head_sha=?"
            ), (tenant_id, repository, int(pull_request), head_sha)).fetchone()
        if not row:
            return None
        value = dict(row)
        value["inline_delivered"] = bool(value["inline_delivered"])
        return value

    def mark_github_inline_delivered(
        self, tenant_id: str, repository: str, pull_request: int, head_sha: str,
    ) -> None:
        with self._write() as conn:
            conn.execute(self._sql(
                "INSERT INTO github_delivery_state"
                "(tenant_id,repository,pull_request,head_sha,inline_delivered,"
                "check_run_id,updated_at) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(tenant_id,repository,pull_request,head_sha) DO UPDATE SET "
                "inline_delivered=excluded.inline_delivered,"
                "updated_at=excluded.updated_at"
            ), (
                tenant_id, repository, int(pull_request), head_sha,
                True, None, utc_now(),
            ))

    def set_github_check_run(
        self, tenant_id: str, repository: str, pull_request: int,
        head_sha: str, check_run_id: int,
    ) -> None:
        with self._write() as conn:
            conn.execute(self._sql(
                "INSERT INTO github_delivery_state"
                "(tenant_id,repository,pull_request,head_sha,inline_delivered,"
                "check_run_id,updated_at) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(tenant_id,repository,pull_request,head_sha) DO UPDATE SET "
                "check_run_id=excluded.check_run_id,"
                "updated_at=excluded.updated_at"
            ), (
                tenant_id, repository, int(pull_request), head_sha,
                False, int(check_run_id), utc_now(),
            ))
