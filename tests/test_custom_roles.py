"""Custom agent role loader tests.

Covers:
- YAML parsing into a valid BaseSDLCAgent subclass
- Required-field validation
- prior_stages() delegation
- Dynamic class naming
- load_custom_roles() scans directory, skips .sample files
- Pipeline registry picks up custom roles
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from agents.custom_loader import _build_class, _build_class_from_file, load_custom_roles


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MINIMAL_SPEC = {
    "role": "test_analyst",
    "display_name": "Test Analyst",
    "authority_statement": "You analyse test results.",
    "output_schema": {
        "type": "object",
        "required": ["findings"],
        "properties": {"findings": {"type": "array", "items": {"type": "string"}}},
    },
}


def _write_yaml(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(textwrap.dedent(content), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# _build_class unit tests
# ---------------------------------------------------------------------------

class TestBuildClass:
    def test_creates_subclass(self):
        from core.agent_base import BaseSDLCAgent
        cls = _build_class(_MINIMAL_SPEC)
        assert issubclass(cls, BaseSDLCAgent)

    def test_role_attribute(self):
        cls = _build_class(_MINIMAL_SPEC)
        assert cls.ROLE == "test_analyst"

    def test_display_name(self):
        cls = _build_class(_MINIMAL_SPEC)
        assert cls.ROLE_DISPLAY == "Test Analyst"

    def test_authority_statement(self):
        cls = _build_class(_MINIMAL_SPEC)
        assert "analyse test results" in cls.AUTHORITY_STATEMENT

    def test_output_schema(self):
        cls = _build_class(_MINIMAL_SPEC)
        assert "findings" in cls.OUTPUT_SCHEMA["properties"]

    def test_prior_stages_empty_by_default(self):
        cls = _build_class(_MINIMAL_SPEC)
        instance = _make_instance(cls)
        assert instance.prior_stages() == []

    def test_prior_stages_from_spec(self):
        spec = {**_MINIMAL_SPEC, "prior_stages": ["discovery", "requirements"]}
        cls = _build_class(spec)
        instance = _make_instance(cls)
        assert instance.prior_stages() == ["discovery", "requirements"]

    def test_max_iterations_default(self):
        cls = _build_class(_MINIMAL_SPEC)
        assert cls.MAX_ITERATIONS == 30

    def test_max_iterations_custom(self):
        spec = {**_MINIMAL_SPEC, "max_iterations": 50}
        cls = _build_class(spec)
        assert cls.MAX_ITERATIONS == 50

    def test_enabled_toolsets_default_empty(self):
        cls = _build_class(_MINIMAL_SPEC)
        assert cls.ENABLED_TOOLSETS == []

    def test_enabled_toolsets_from_spec(self):
        spec = {**_MINIMAL_SPEC, "enabled_toolsets": ["read_file", "terminal"]}
        cls = _build_class(spec)
        assert "terminal" in cls.ENABLED_TOOLSETS

    def test_dynamic_class_name(self):
        cls = _build_class(_MINIMAL_SPEC)
        assert "test_analyst" in cls.__name__

    def test_missing_role_raises(self):
        bad = {k: v for k, v in _MINIMAL_SPEC.items() if k != "role"}
        with pytest.raises(ValueError, match="role"):
            _build_class(bad)

    def test_missing_output_schema_raises(self):
        bad = {k: v for k, v in _MINIMAL_SPEC.items() if k != "output_schema"}
        with pytest.raises(ValueError, match="output_schema"):
            _build_class(bad)

    def test_skill_md_path(self):
        spec = {**_MINIMAL_SPEC, "skill_md_path": "skills/test/SKILL.md"}
        cls = _build_class(spec)
        assert cls.SKILL_MD_PATH == "skills/test/SKILL.md"


def _make_instance(cls):
    from unittest.mock import MagicMock
    from core.agent_base import AgentConfig
    from core.memory import ArtifactBundle, ProjectMemory
    cfg = AgentConfig(role=cls.ROLE)
    pm = MagicMock(spec=ProjectMemory)
    ab = ArtifactBundle("run-test")
    db = MagicMock()
    return cls(config=cfg, project_memory=pm, artifact_bundle=ab, db=db)


# ---------------------------------------------------------------------------
# _build_class_from_file
# ---------------------------------------------------------------------------

class TestBuildClassFromFile:
    def test_load_minimal_yaml(self, tmp_path):
        yaml_content = """\
            role: yaml_analyst
            display_name: YAML Analyst
            authority_statement: You analyse YAML.
            output_schema:
              type: object
              required: [result]
              properties:
                result: {type: string}
        """
        p = _write_yaml(tmp_path, "yaml_analyst.yaml", yaml_content)
        cls = _build_class_from_file(p)
        assert cls.ROLE == "yaml_analyst"

    def test_full_yaml_spec(self, tmp_path):
        yaml_content = """\
            role: data_engineer
            display_name: Data Engineer
            authority_statement: You build pipelines.
            max_iterations: 40
            enabled_toolsets: [read_file, terminal]
            prior_stages: [discovery, requirements]
            output_schema:
              type: object
              required: [pipelines]
              properties:
                pipelines: {type: array, items: {type: string}}
        """
        p = _write_yaml(tmp_path, "data_engineer.yaml", yaml_content)
        cls = _build_class_from_file(p)
        assert cls.MAX_ITERATIONS == 40
        assert "terminal" in cls.ENABLED_TOOLSETS
        instance = _make_instance(cls)
        assert "discovery" in instance.prior_stages()


# ---------------------------------------------------------------------------
# load_custom_roles
# ---------------------------------------------------------------------------

class TestLoadCustomRoles:
    def test_returns_empty_for_missing_dir(self, tmp_path):
        result = load_custom_roles(tmp_path / "nonexistent")
        assert result == {}

    def test_skips_sample_files(self, tmp_path):
        (tmp_path / "example.yaml.sample").write_text("role: skip_me\n", encoding="utf-8")
        result = load_custom_roles(tmp_path)
        assert result == {}

    def test_loads_valid_yaml(self, tmp_path):
        yaml_content = """\
            role: custom_qa
            display_name: Custom QA
            authority_statement: Run tests.
            output_schema:
              type: object
              required: [passed]
              properties:
                passed: {type: boolean}
        """
        _write_yaml(tmp_path, "custom_qa.yaml", yaml_content)
        result = load_custom_roles(tmp_path)
        assert "custom_qa" in result

    def test_skips_invalid_yaml_and_continues(self, tmp_path):
        _write_yaml(tmp_path, "broken.yaml", "role: bad_role\n# missing required fields\n")
        yaml_content = """\
            role: good_role
            display_name: Good
            authority_statement: Works fine.
            output_schema:
              type: object
              required: [ok]
              properties:
                ok: {type: boolean}
        """
        _write_yaml(tmp_path, "good.yaml", yaml_content)
        result = load_custom_roles(tmp_path)
        assert "good_role" in result
        assert "bad_role" not in result

    def test_multiple_roles_loaded(self, tmp_path):
        for i in range(3):
            content = f"""\
                role: role_{i}
                display_name: Role {i}
                authority_statement: Role {i}.
                output_schema:
                  type: object
                  required: [x]
                  properties:
                    x: {{type: string}}
            """
            _write_yaml(tmp_path, f"role_{i}.yaml", content)
        result = load_custom_roles(tmp_path)
        assert len(result) == 3

    def test_pipeline_picks_up_custom_roles(self, tmp_path):
        yaml_content = """\
            role: pipeline_test_role
            display_name: Pipeline Test
            authority_statement: Test pipeline integration.
            output_schema:
              type: object
              required: [done]
              properties:
                done: {type: boolean}
        """
        _write_yaml(tmp_path, "pipeline_test_role.yaml", yaml_content)

        import agents.custom_loader as cl
        original = cl._CUSTOM_ROLES_DIR
        cl._CUSTOM_ROLES_DIR = tmp_path
        try:
            from core.pipeline import SDLCPipeline
            pipeline = SDLCPipeline.from_dict({"workspace": str(tmp_path / "out")})
            assert "pipeline_test_role" in pipeline._agent_registry
        finally:
            cl._CUSTOM_ROLES_DIR = original
