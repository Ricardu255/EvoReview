"""Tests for skill-scoped tool permissions and resources."""
import os
import tempfile
import unittest

from evoagent.agent.repository_tools import RepositoryToolSuite
from evoagent.agent.tools import ToolPolicy
from evoagent.domain.diff_parser import parse_unified_diff
from evoagent.prompts import ROLE_PERMISSIONS
from evoagent.agent.runtime import ToolRegistry


class FakeSkill:
    def __init__(self, name="review-dangerous-calls", paths=("SKILL.md",)):
        self.name = name
        self.allowed_tools = {"read_file"}
        self.resource_paths = paths

    def read_resource(self, path):
        return "resource:%s:%s" % (self.name, path)


class AgentToolPolicyTests(unittest.TestCase):
    def test_permissions_intersect_all_skill_restrictions(self):
        base = set(ROLE_PERMISSIONS["security"])
        self.assertEqual(base, ToolPolicy._skill_tool_permissions("security", []))
        self.assertEqual(
            base.intersection({"read_file"}),
            ToolPolicy._skill_tool_permissions("security", [FakeSkill()]),
        )

    def test_register_skill_resource_tool_only_when_resources_exist(self):
        tools = ToolRegistry()
        ToolPolicy._register_skill_resource_tool(tools, [FakeSkill(paths=())])
        self.assertEqual([], tools.names())

        skill = FakeSkill()
        ToolPolicy._register_skill_resource_tool(tools, [skill])
        self.assertEqual(["read_skill_resource"], tools.names())
        self.assertEqual(
            {"skill": "review-dangerous-calls", "path": "SKILL.md",
             "content": "resource:review-dangerous-calls:SKILL.md"},
            tools.invoke("read_skill_resource", {
                "skill": "review-dangerous-calls", "path": "SKILL.md",
            }),
        )
        with self.assertRaises(PermissionError):
            tools.invoke("read_skill_resource", {
                "skill": "other-skill", "path": "SKILL.md",
            })


class SensitiveRepositoryPathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = self.temp.name
        self._write(".env", "EVOAGENT_AUTH_SECRET=real-secret\n")
        self._write(".env.example", "EVOAGENT_AUTH_SECRET=\n")
        self._write("notes.txt", "public\n")
        self._write(os.path.join("nested", "id_rsa"), "private-key\n")
        self.suite = RepositoryToolSuite(self.root, "", parse_unified_diff(""))

    def _write(self, name, content):
        path = os.path.join(self.root, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)

    def test_secret_files_are_not_listed(self):
        files = self.suite.list_repository()["output"]["files"]
        self.assertIn("notes.txt", files)
        self.assertIn(".env.example", files)
        self.assertNotIn(".env", files)
        self.assertNotIn("nested/id_rsa", files)

    def test_secret_files_cannot_be_read_directly_or_via_traversal(self):
        for path in (".env", "nested/id_rsa", "nested/../.env"):
            with self.assertRaises(ValueError, msg=path):
                self.suite.read_file(path)

    def test_example_templates_remain_readable(self):
        result = self.suite.read_file(".env.example")
        self.assertIn("EVOAGENT_AUTH_SECRET", result["output"]["content"])

    def test_search_does_not_return_secret_lines(self):
        hits = self.suite.search_repository("real-secret")["output"]
        self.assertEqual([], hits)


if __name__ == "__main__":
    unittest.main()
