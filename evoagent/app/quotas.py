"""Per-tenant daily review, token and cost quotas."""
from datetime import datetime, timezone
import logging
from typing import Any, Dict, Optional

from ..errors import QuotaExceeded


logger = logging.getLogger("evoagent")


def _day(now: Optional[datetime] = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")


class TenantQuotaService:
    def __init__(self, store: Any, settings: Any):
        self.store = store
        self.defaults = {
            "daily_reviews": max(0, int(settings.tenant_daily_review_limit)),
            "daily_tokens": max(0, int(settings.tenant_daily_token_limit)),
            "daily_cost_usd": max(0.0, float(settings.tenant_daily_cost_limit_usd)),
        }

    def status(self, tenant_id: str, day: Optional[str] = None) -> Dict[str, Any]:
        day = day or _day()
        quota = self.store.get_tenant_quota(tenant_id) or {
            "tenant_id": tenant_id, **self.defaults,
        }
        usage = self.store.get_tenant_usage(tenant_id, day)
        limits = {
            "daily_reviews": int(quota.get("daily_reviews", 0)),
            "daily_tokens": int(quota.get("daily_tokens", 0)),
            "daily_cost_usd": float(quota.get("daily_cost_usd", 0.0)),
        }
        remaining = {}
        for key, used_key in (
            ("daily_reviews", "reviews"),
            ("daily_tokens", "tokens"),
            ("daily_cost_usd", "cost_usd"),
        ):
            limit = limits[key]
            remaining[key] = (
                None if limit <= 0 else max(0, round(limit - usage[used_key], 6))
                if key == "daily_cost_usd" else max(0, limit - usage[used_key])
            )
        return {
            "tenant_id": tenant_id, "day": day,
            "limits": limits, "usage": usage, "remaining": remaining,
            "unlimited": {key: limits[key] <= 0 for key in limits},
        }

    def set_limits(
        self, tenant_id: str, daily_reviews: int = 0,
        daily_tokens: int = 0, daily_cost_usd: float = 0.0,
    ) -> Dict[str, Any]:
        quota = self.store.set_tenant_quota(
            tenant_id, daily_reviews, daily_tokens, daily_cost_usd,
        )
        return self.status(tenant_id)

    def reserve_review(self, tenant_id: str, day: Optional[str] = None) -> None:
        if not self.store.consume_tenant_quota(
            tenant_id, day or _day(), reviews=1, defaults=self.defaults,
            enforce=True,
        ):
            raise QuotaExceeded(
                "tenant daily review, token or cost quota is exhausted"
            )

    def record_usage(
        self, tenant_id: str, tokens: int, cost_usd: float,
        day: Optional[str] = None,
    ) -> None:
        try:
            self.store.consume_tenant_quota(
                tenant_id, day or _day(), reviews=0,
                tokens=max(0, int(tokens)), cost_usd=max(0.0, float(cost_usd)),
                defaults=self.defaults, enforce=False,
            )
        except Exception:
            # Usage accounting must never turn a completed review into a failure.
            logger.exception("failed to record tenant quota usage")
