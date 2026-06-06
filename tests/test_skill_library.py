"""Skill library tests.

Covers SkillLibrary (list/get/exists) and the skill_view / skill_list tools.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.skill_library import SkillLibrary


@pytest.fixture()
def lib_dir(tmp_path: Path) -> Path:
    """A temp directory with three skill fragments."""
    d = tmp_path / "skills" / "library"
    d.mkdir(parents=True)
    (d / "rest-api.md").write_text("# REST API\nUse nouns for resources.", encoding="utf-8")
    (d / "event-driven.md").write_text("# Events\nImmutable records of what happened.", encoding="utf-8")
    (d / "microservices.md").write_text("# Microservices\nOwn your data.", encoding="utf-8")
    return d


@pytest.fixture()
def lib(lib_dir: Path) -> SkillLibrary:
    return SkillLibrary(library_dir=lib_dir)


class TestSkillLibraryList:
    def test_lists_all_skills(self, lib):
        skills = lib.list_skills()
        names = {s["name"] for s in skills}
        assert names == {"rest-api", "event-driven", "microservices"}

    def test_each_skill_has_summary(self, lib):
        for s in lib.list_skills():
            assert s["summary"], f"Expected summary for {s['name']}"

    def test_each_skill_has_path(self, lib):
        for s in lib.list_skills():
            assert Path(s["path"]).exists()

    def test_empty_library_dir(self, tmp_path):
        empty = SkillLibrary(library_dir=tmp_path / "empty")
        assert empty.list_skills() == []

    def test_skills_sorted_alphabetically(self, lib):
        names = [s["name"] for s in lib.list_skills()]
        assert names == sorted(names)


class TestSkillLibraryGet:
    def test_get_existing_skill(self, lib):
        content = lib.get_skill("rest-api")
        assert content is not None
        assert "REST" in content

    def test_get_nonexistent_skill_returns_none(self, lib):
        assert lib.get_skill("does-not-exist") is None

    def test_get_returns_full_content(self, lib):
        content = lib.get_skill("event-driven")
        assert "Immutable records" in content

    def test_skill_exists_true(self, lib):
        assert lib.skill_exists("microservices") is True

    def test_skill_exists_false(self, lib):
        assert lib.skill_exists("missing") is False


class TestSkillTools:
    def test_skill_list_tool(self, lib):
        import json
        from core.registry import ToolRegistry
        from core.skill_library import get_skill_library

        # Use the library fixture directly, bypassing the module singleton
        import tools.skill_tools  # ensure registered
        import core.skill_library as sl_mod

        original = sl_mod._default_library
        sl_mod._default_library = lib
        try:
            from tools.skill_tools import _skill_list
            result = json.loads(_skill_list({}))
            assert "skills" in result
            names = {s["name"] for s in result["skills"]}
            assert "rest-api" in names
        finally:
            sl_mod._default_library = original

    def test_skill_view_tool_found(self, lib):
        import json
        import core.skill_library as sl_mod

        import tools.skill_tools  # ensure registered

        original = sl_mod._default_library
        sl_mod._default_library = lib
        try:
            from tools.skill_tools import _skill_view
            result = json.loads(_skill_view({"name": "rest-api"}))
            assert "content" in result
            assert "REST" in result["content"]
        finally:
            sl_mod._default_library = original

    def test_skill_view_tool_not_found(self, lib):
        import json
        import core.skill_library as sl_mod

        import tools.skill_tools  # ensure registered

        original = sl_mod._default_library
        sl_mod._default_library = lib
        try:
            from tools.skill_tools import _skill_view
            result = json.loads(_skill_view({"name": "nonexistent"}))
            assert "error" in result
            assert "available" in result
        finally:
            sl_mod._default_library = original

    def test_skill_view_tool_missing_name(self, lib):
        import json
        from tools.skill_tools import _skill_view
        result = json.loads(_skill_view({}))
        assert "error" in result

    def test_built_in_fragments_exist(self):
        from core.skill_library import _LIBRARY_DIR
        assert (_LIBRARY_DIR / "rest-api.md").exists()
        assert (_LIBRARY_DIR / "event-driven.md").exists()
        assert (_LIBRARY_DIR / "microservices.md").exists()

    def test_skill_tools_registered(self):
        from core.registry import registry
        import tools.skill_tools  # noqa
        defs = registry.get_definitions(enabled_toolsets=["skills"])
        names = {d["function"]["name"] for d in defs}
        assert "skill_list" in names
        assert "skill_view" in names
