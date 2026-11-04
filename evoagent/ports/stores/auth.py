"""Users, repository grants and the admin audit log."""
from typing import Any, Dict, Optional, Protocol, runtime_checkable


@runtime_checkable
class AuthStorePort(Protocol):
    def create_user(
        self, user_id: str, username: str, password_hash: str,
        tenant_id: str, role: str, platform_admin: bool = False,
    ) -> None:
        ...

    def set_platform_admin(self, username: str, enabled: bool) -> bool:
        ...

    def create_tenant(self, tenant_id: str) -> bool:
        ...

    def list_tenants(self) -> list:
        ...

    def tenant_exists(self, tenant_id: str) -> bool:
        ...

    def get_user(self, username: str) -> Optional[Dict[str, Any]]:
        ...

    def list_users(self, tenant_id: str) -> list:
        ...

    def update_user_password(
        self, username: str, tenant_id: str, password_hash: str,
    ) -> bool:
        ...

    def set_user_active(
        self, username: str, tenant_id: str, active: bool,
    ) -> bool:
        ...

    def grant_repository(
        self, tenant_id: str, repository: str, auto_fix: bool = False,
    ) -> None:
        ...

    def revoke_repository(self, tenant_id: str, repository: str) -> bool:
        ...

    def list_repository_grants(self, tenant_id: str) -> list:
        ...

    def has_repository_grants(self, tenant_id: str) -> bool:
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
