"""GitHub installations and idempotent webhook delivery."""
from typing import Any, Dict, Optional, Protocol, runtime_checkable


@runtime_checkable
class WebhookStorePort(Protocol):
    def save_installation(
        self, installation_id: int, account_login: str,
        tenant_id: str = "default",
    ) -> None:
        ...

    def installation_tenant(self, installation_id: int) -> Optional[str]:
        ...

    def claim_webhook(
        self, delivery_id: str, tenant_id: str, event_type: str,
        payload_sha256: str, claim_token: str = "", lease_seconds: int = 300,
    ) -> bool:
        ...

    def complete_webhook(
        self, delivery_id: str, task_id: Optional[str], claim_token: Optional[str] = None,
    ) -> None:
        ...

    def bind_webhook_task(
        self, delivery_id: str, task_id: str, claim_token: str,
    ) -> None:
        ...

    def release_webhook(self, delivery_id: str, claim_token: str) -> None:
        ...

    def get_webhook(self, delivery_id: str) -> Optional[Dict[str, Any]]:
        ...
