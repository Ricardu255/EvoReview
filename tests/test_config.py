import os
import tempfile
import unittest
from unittest.mock import patch

from evoagent.config import Settings, load_dotenv


class TestExecutionSettingTests(unittest.TestCase):
    def test_test_execution_is_enabled_by_default_and_can_be_disabled(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(Settings.from_env().test_execution_allow_untrusted)
        with patch.dict(
            os.environ, {"EVOAGENT_TEST_EXECUTION_ALLOW_UNTRUSTED": "false"},
            clear=True,
        ):
            self.assertFalse(Settings.from_env().test_execution_allow_untrusted)


class DotenvTests(unittest.TestCase):
    def test_loads_valid_assignments_and_quoted_values(self):
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
            handle.write("# comment\n")
            handle.write("export EVOAGENT_LLM_PROVIDER=deepseek\n")
            handle.write('EVOAGENT_DEEPSEEK_API_KEY="test-key"\n')
            handle.write("invalid line\n")
            path = handle.name
        try:
            with patch.dict(os.environ, {}, clear=True):
                load_dotenv([path])
                self.assertEqual("deepseek", os.environ["EVOAGENT_LLM_PROVIDER"])
                self.assertEqual("test-key", os.environ["EVOAGENT_DEEPSEEK_API_KEY"])
        finally:
            os.unlink(path)

    def test_process_environment_has_priority(self):
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
            handle.write("EVOAGENT_LLM_PROVIDER=deepseek\n")
            path = handle.name
        try:
            with patch.dict(os.environ, {"EVOAGENT_LLM_PROVIDER": "custom"}, clear=True):
                load_dotenv([path])
                self.assertEqual("custom", os.environ["EVOAGENT_LLM_PROVIDER"])
        finally:
            os.unlink(path)
