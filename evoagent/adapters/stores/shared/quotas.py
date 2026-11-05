"""Tenant daily quota and usage rows shared by both SQL backends."""
from typing import Any, Dict, Optional

from ....time_utils import utc_now


class QuotaStoreMixin:
    def get_tenant_quota(self, tenant_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(self._sql(
                "SELECT tenant_id,daily_reviews,daily_tokens,daily_cost_usd,updated_at "
                "FROM tenant_quotas WHERE tenant_id=?"
            ), (tenant_id,)).fetchone()
        return dict(row) if row else None

    def set_tenant_quota(
        self, tenant_id: str, daily_reviews: int, daily_tokens: int,
        daily_cost_usd: float,
    ) -> Dict[str, Any]:
        with self._write() as conn:
            conn.execute(self._sql(
                "INSERT INTO tenant_quotas"
                "(tenant_id,daily_reviews,daily_tokens,daily_cost_usd,updated_at) "
                "VALUES (?,?,?,?,?) ON CONFLICT(tenant_id) DO UPDATE SET "
                "daily_reviews=excluded.daily_reviews,"
                "daily_tokens=excluded.daily_tokens,"
                "daily_cost_usd=excluded.daily_cost_usd,"
                "updated_at=excluded.updated_at"
            ), (
                tenant_id, max(0, int(daily_reviews)),
                max(0, int(daily_tokens)), max(0.0, float(daily_cost_usd)),
                utc_now(),
            ))
        return {
            "tenant_id": tenant_id,
            "daily_reviews": max(0, int(daily_reviews)),
            "daily_tokens": max(0, int(daily_tokens)),
            "daily_cost_usd": max(0.0, float(daily_cost_usd)),
        }

    def get_tenant_usage(self, tenant_id: str, day: str) -> Dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(self._sql(
                "SELECT reviews,tokens,cost_usd FROM tenant_usage "
                "WHERE tenant_id=? AND day=?"
            ), (tenant_id, day)).fetchone()
        if not row:
            return {
                "tenant_id": tenant_id, "day": day,
                "reviews": 0, "tokens": 0, "cost_usd": 0.0,
            }
        value = dict(row)
        return {
            "tenant_id": tenant_id, "day": day,
            "reviews": int(value["reviews"]),
            "tokens": int(value["tokens"]),
            "cost_usd": float(value["cost_usd"]),
        }

    def consume_tenant_quota(
        self, tenant_id: str, day: str, reviews: int = 0, tokens: int = 0,
        cost_usd: float = 0.0, defaults: Optional[Dict[str, Any]] = None,
        enforce: bool = True,
    ) -> bool:
        reviews = max(0, int(reviews))
        tokens = max(0, int(tokens))
        cost_usd = max(0.0, float(cost_usd))
        defaults = defaults or {}
        with self._write() as conn:
            self._acquire_key_lock(conn, "quota:%s:%s" % (tenant_id, day))
            limits = conn.execute(self._sql(
                "SELECT daily_reviews,daily_tokens,daily_cost_usd "
                "FROM tenant_quotas WHERE tenant_id=?"
            ), (tenant_id,)).fetchone()
            if limits:
                review_limit = int(limits["daily_reviews"])
                token_limit = int(limits["daily_tokens"])
                cost_limit = float(limits["daily_cost_usd"])
            else:
                review_limit = int(defaults.get("daily_reviews", 0))
                token_limit = int(defaults.get("daily_tokens", 0))
                cost_limit = float(defaults.get("daily_cost_usd", 0.0))
            usage = conn.execute(self._sql(
                "SELECT reviews,tokens,cost_usd FROM tenant_usage "
                "WHERE tenant_id=? AND day=?"
            ), (tenant_id, day)).fetchone()
            used_reviews = int(usage["reviews"]) if usage else 0
            used_tokens = int(usage["tokens"]) if usage else 0
            used_cost = float(usage["cost_usd"]) if usage else 0.0
            if enforce:
                if review_limit > 0 and used_reviews + reviews > review_limit:
                    return False
                if token_limit > 0 and used_tokens + tokens > token_limit:
                    return False
                if cost_limit > 0 and used_cost + cost_usd > cost_limit:
                    return False
            conn.execute(self._sql(
                "INSERT INTO tenant_usage(tenant_id,day,reviews,tokens,cost_usd) "
                "VALUES (?,?,?,?,?) ON CONFLICT(tenant_id,day) DO UPDATE SET "
                "reviews=tenant_usage.reviews+excluded.reviews,"
                "tokens=tenant_usage.tokens+excluded.tokens,"
                "cost_usd=tenant_usage.cost_usd+excluded.cost_usd"
            ), (tenant_id, day, reviews, tokens, cost_usd))
        return True
