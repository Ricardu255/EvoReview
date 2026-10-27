"""Progressive delivery, shadow observations and alerts."""
from typing import Any, Dict, Optional, Protocol, runtime_checkable


@runtime_checkable
class ObservabilityStorePort(Protocol):
    def save_deployment(
        self, tenant_id: str, skill_name: str, config: Dict[str, Any],
    ) -> None:
        ...

    def get_deployment(
        self, tenant_id: str, skill_name: str,
    ) -> Optional[Dict[str, Any]]:
        ...

    def record_deployment_result(
        self, tenant_id: str, skill_name: str, failed: bool,
    ) -> Optional[Dict[str, Any]]:
        ...

    def record_shadow_observation(
        self, tenant_id: str, skill_name: str, task_id: str, lane: str,
        primary: Dict[str, Any], candidate: Optional[Dict[str, Any]],
        disagreement: float, candidate_failed: bool = False,
    ) -> Optional[Dict[str, Any]]:
        ...

    def list_release_observations(
        self, tenant_id: str, skill_name: str, limit: int = 100,
    ) -> list:
        ...

    def create_alert(
        self, tenant_id: str, alert_key: str, severity: str, message: str,
    ) -> None:
        ...

    def list_alerts(self, tenant_id: str, limit: int = 100) -> list:
        ...

    def dashboard_stats(
        self, tenant_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        ...
