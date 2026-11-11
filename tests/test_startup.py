"""Startup, CLI and bind-address behavior that used to fail silently."""
import contextlib
import io
import os
import socket
import subprocess
import sys
import unittest
from dataclasses import replace
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from evoagent.__main__ import main
from evoagent.api import server_class
from evoagent.config import Settings


ROOT = Path(__file__).resolve().parents[1]


class CommandLineTests(unittest.TestCase):
    def test_help_exits_without_starting_the_server(self):
        with self.assertRaises(SystemExit) as raised:
            main(["--help"])
        self.assertEqual(0, raised.exception.code)

    def test_version_exits_zero(self):
        with self.assertRaises(SystemExit) as raised:
            main(["--version"])
        self.assertEqual(0, raised.exception.code)

    def test_check_validates_configuration_without_binding(self):
        output = io.StringIO()
        with patch.dict(os.environ, {}, clear=True):
            with contextlib.redirect_stdout(output):
                main(["--check"])
        self.assertEqual("configuration ok\n", output.getvalue())

    def test_module_entry_point_prints_help(self):
        proc = subprocess.run(
            [sys.executable, "-m", "evoagent", "--help"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("usage: evoagent", proc.stdout)


class ServerBindingTests(unittest.TestCase):
    def test_default_host_uses_the_ipv4_server(self):
        self.assertIs(ThreadingHTTPServer, server_class("127.0.0.1"))
        self.assertIs(ThreadingHTTPServer, server_class("localhost"))

    def test_ipv6_literal_uses_the_ipv6_server(self):
        self.assertEqual(socket.AF_INET6, server_class("::1").address_family)


class StartupValidationTests(unittest.TestCase):
    def settings(self):
        with patch.dict(os.environ, {}, clear=True):
            return Settings.from_env()

    def test_port_outside_the_socket_range_is_rejected(self):
        for port in (0, -1, 70000):
            with self.assertRaises(ValueError):
                replace(self.settings(), port=port).validate_evolution()

    def test_blank_host_is_rejected(self):
        with self.assertRaises(ValueError):
            replace(self.settings(), host="  ").validate_evolution()

    def test_out_of_range_limits_are_rejected(self):
        for overrides in (
            {"report_max_findings": 0},
            {"max_steps": 0},
            {"timeout_seconds": 0},
            {"async_workers": 0},
            {"queue_max_attempts": 0},
            {"queue_stream_maxlen": 0},
            {"queue_dlq_maxlen": 0},
            {"alert_window_seconds": 0},
            {"session_ttl_seconds": 0},
            {"agent_context_input_tokens": 0},
        ):
            with self.assertRaises(ValueError, msg=str(overrides)):
                replace(self.settings(), **overrides).validate_evolution()

    def test_agent_token_budget_must_fit_the_context_window(self):
        with self.assertRaises(ValueError):
            replace(
                self.settings(),
                agent_context_window_tokens=10000,
                agent_context_input_tokens=9000,
                agent_token_budget=5000,
            ).validate_evolution()

    def test_proxied_deployment_must_enable_authentication(self):
        # A loopback-bound console can still be exposed by a tunnel/reverse
        # proxy, which the non-loopback host check cannot detect.
        with self.assertRaisesRegex(ValueError, "EVOAGENT_AUTH_REQUIRED"):
            replace(
                self.settings(), trusted_proxies=("127.0.0.1",)
            ).validate_evolution()
        replace(
            self.settings(), trusted_proxies=("127.0.0.1",),
            auth_required=True, auth_secret="a" * 32,
        ).validate_evolution()

    def test_invalid_trusted_proxy_is_rejected(self):
        with self.assertRaises(ValueError):
            replace(self.settings(), trusted_proxies=("not-a-cidr",)).validate_evolution()
        with patch.dict(os.environ, {"EVOAGENT_TRUSTED_PROXIES": "nope"}, clear=True):
            with self.assertRaises(ValueError):
                Settings.from_env()


if __name__ == "__main__":
    unittest.main()
