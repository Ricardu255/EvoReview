"""Tests for skill-scoped tool permissions and resources."""
import unittest

from evoagent.agent.tools import ToolPolicy
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


if __name__ == "__main__":
    unittest.main()
