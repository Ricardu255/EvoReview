"""One-time GitHub App setup state persistence."""
from typing import Optional, Protocol


class SetupStateStorePort(Protocol):
    def save_github_setup_state(
        self, token_hash: str, tenant_id: str, expires_at: float,
    ) -> None:
        ...

    def consume_github_setup_state(self, token_hash: str) -> Optional[str]:
        ...
