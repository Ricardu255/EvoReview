"""Role tool permissions and Skill resource tools."""
from .collaborators import RuntimeBackref
from ..prompts import ROLE_PERMISSIONS
from .runtime import AgentTool


class ToolPolicy(RuntimeBackref):
    @staticmethod
    def _skill_tool_permissions(worker, skills):
        base = set(ROLE_PERMISSIONS[worker])
        restrictions = [set(skill.allowed_tools) for skill in skills if skill.allowed_tools]
        if not restrictions:
            return base
        return base.intersection(set().union(*restrictions))

    @staticmethod
    def _register_skill_resource_tool(tools, selected_skills):
        if not any(skill.resource_paths for skill in selected_skills):
            return
        by_name = {skill.name: skill for skill in selected_skills}

        def read_skill_resource(skill: str, path: str):
            selected = by_name.get(skill)
            if selected is None:
                raise PermissionError("Agent Skill was not selected for this assignment")
            return {
                "skill": skill, "path": path,
                "content": selected.read_resource(path),
            }

        tools.register(AgentTool(
            "read_skill_resource",
            "Read one supporting text resource from an active Agent Skill.",
            {
                "type": "object",
                "properties": {
                    "skill": {"type": "string"}, "path": {"type": "string"},
                },
                "required": ["skill", "path"], "additionalProperties": False,
            },
            read_skill_resource,
        ))
