"""Users, repository grants and the admin audit log."""
from typing import Any, Dict, Optional, Protocol, runtime_checkable


@runtime_checkable
class AuthStorePort(Protocol):
    def create_user(
        self, user_id: str, username: str, password_hash: str,
        tenant_id: str, role: str,
    ) -> None:
        ...

    def get_user(self, username: str) -> Optional[Dict[str, Any]]:
        ...

    def grant_repository(
        self, tenant_id: str, repository: str, auto_fix: bool = False,
    ) -> None:
        ...

    def repository_allowed(
        self, tenant_id: str, repository: str, require_auto_fix: bool = False,
    ) -> bool:
        ...

    def audit(
        self, tenant_id: str, actor: str, action: str, resource: str,
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        ...

    def list_audit(self, tenant_id: str, limit: int = 100) -> list:
        ...
