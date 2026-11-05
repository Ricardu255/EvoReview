"""Tenant daily quota service tests."""
from types import SimpleNamespace
import unittest

from evoagent.adapters.stores.sqlite.store import TaskStore
from evoagent.app.quotas import TenantQuotaService
from evoagent.errors import QuotaExceeded


class TenantQuotaTests(unittest.TestCase):
    def setUp(self):
        self.store = TaskStore(":memory:")
        self.addCleanup(self.store.close)
        self.settings = SimpleNamespace(
            tenant_daily_review_limit=1,
            tenant_daily_token_limit=100,
            tenant_daily_cost_limit_usd=0.5,
        )
        self.quota = TenantQuotaService(self.store, self.settings)

    def test_review_limit_is_atomic_and_status_is_visible(self):
        self.quota.reserve_review("tenant-a")
        status = self.quota.status("tenant-a")
        self.assertEqual(1, status["usage"]["reviews"])
        self.assertEqual(0, status["remaining"]["daily_reviews"])
        with self.assertRaises(QuotaExceeded):
            self.quota.reserve_review("tenant-a")

    def test_token_and_cost_usage_is_recorded(self):
        self.quota.record_usage("tenant-a", tokens=120, cost_usd=0.75)
        status = self.quota.status("tenant-a")
        self.assertEqual(120, status["usage"]["tokens"])
        self.assertEqual(0.75, status["usage"]["cost_usd"])
        self.assertEqual(0, status["remaining"]["daily_tokens"])
        self.assertEqual(0.0, status["remaining"]["daily_cost_usd"])
        with self.assertRaises(QuotaExceeded):
            self.quota.reserve_review("tenant-a")

    def test_per_tenant_override_and_zero_means_unlimited(self):
        self.quota.set_limits("tenant-a", 0, 0, 0.0)
        self.quota.reserve_review("tenant-a")
        self.quota.reserve_review("tenant-a")
        self.quota.record_usage("tenant-a", tokens=10_000, cost_usd=100.0)
        status = self.quota.status("tenant-a")
        self.assertTrue(status["unlimited"]["daily_reviews"])
        self.assertTrue(status["unlimited"]["daily_tokens"])
        self.assertTrue(status["unlimited"]["daily_cost_usd"])


if __name__ == "__main__":
    unittest.main()
