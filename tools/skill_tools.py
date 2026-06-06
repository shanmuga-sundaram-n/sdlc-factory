"""Skill library tools — registered in the 'skills' toolset.

Agents with 'skills' in their ENABLED_TOOLSETS can call:
  - skill_list: enumerate all available pattern fragments
  - skill_view: retrieve the full content of a named fragment
"""

from __future__ import annotations

import json

from core.registry import registry
from core.skill_library import get_skill_library


def _skill_list(args: dict) -> str:
    library = get_skill_library()
    skills = library.list_skills()
    if not skills:
        return json.dumps({"skills": [], "message": "No skill fragments found in library."})
    return json.dumps({"skills": skills})


def _skill_view(args: dict) -> str:
    name = args.get("name", "").strip()
    if not name:
        return json.dumps({"error": "name is required"})
    library = get_skill_library()
    content = library.get_skill(name)
    if content is None:
        available = [s["name"] for s in library.list_skills()]
        return json.dumps({
            "error": f"Skill {name!r} not found",
            "available": available,
        })
    return json.dumps({"name": name, "content": content})


registry.register(
    name="skill_list",
    handler=_skill_list,
    schema={
        "type": "function",
        "function": {
            "name": "skill_list",
            "description": (
                "List all available skill library fragments. "
                "Returns names and one-line summaries."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    toolset="skills",
)

registry.register(
    name="skill_view",
    handler=_skill_view,
    schema={
        "type": "function",
        "function": {
            "name": "skill_view",
            "description": (
                "Retrieve the full content of a named skill library fragment. "
                "Use skill_list first to see available names."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Skill fragment name (e.g. 'rest-api', 'event-driven', 'microservices')",
                    },
                },
                "required": ["name"],
            },
        },
    },
    toolset="skills",
)
