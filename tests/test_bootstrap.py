"""Tests for the composition root and service wiring."""
import os
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch

from evoagent.bootstrap import ServiceContainer, build_container
from evoagent.config import Settings
from evoagent.service import ReviewService


def make_settings(path):
    return Settings(
        host="127.0.0.1", port=8080, db_path=path, max_diff_bytes=10000,
        max_steps=8, timeout_seconds=10, llm_base_url="", llm_api_key="",
        llm_model="", github_webhook_secret="", github_token="",
        auto_post_review=False,
    )


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.settings = make_settings(self.path)

    def tearDown(self):
        os.unlink(self.path)

    def test_refuses_shipped_default_credentials(self):
        settings = replace(
            make_settings(self.path), auth_required=True,
            auth_secret="local-only-change-this-secret-123456",
        )
        with self.assertRaisesRegex(ValueError, "shipped default"):
            settings.validate_evolution()

        settings = replace(
            make_settings(self.path), auth_required=True,
            auth_secret="x" * 32, bootstrap_admin_username="admin",
            bootstrap_admin_password="evoagent-local-admin",
        )
        with self.assertRaisesRegex(ValueError, "shipped default"):
            settings.validate_evolution()

    def test_refuses_non_loopback_binding_without_auth(self):
        settings = replace(make_settings(self.path), host="0.0.0.0")
        with self.assertRaisesRegex(ValueError, "not loopback"):
            settings.validate_evolution()

    def test_build_container_constructs_one_of_each_dependency(self):
        container = build_container(self.settings)
        self.assertIsInstance(container, ServiceContainer)
        self.assertIs(container.settings, self.settings)
        self.assertIsNotNone(container.store)
        self.assertIsNotNone(container.reviewer)
        self.assertIsNotNone(container.harness)
        queue = container.build_queue(
            lambda _payload: None, lambda _payload, _error: None,
        )
        self.assertTrue(queue.backend.startswith("memory"))
        queue.close()

    def test_review_service_uses_the_container(self):
        container = build_container(self.settings)
        with patch("evoagent.service.build_container", return_value=container) as mocked:
            service = ReviewService(self.settings)
        mocked.assert_called_once_with(self.settings)
        self.assertIs(service.store, container.store)
        self.assertIs(service.reviewer, container.reviewer)
        self.assertIs(service.harness, container.harness)
        self.assertTrue(service.queue.backend.startswith("memory"))
        service.queue.close()


if __name__ == "__main__":
    unittest.main()
