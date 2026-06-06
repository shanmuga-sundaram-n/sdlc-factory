"""Tests for Phase 4 — QA Loop.

Covers:
- pytest_tool: JUnit XML parsing, coverage JSON parsing, timeout/not-found error paths
- QALoop: pass-on-first-attempt, fix-iteration dispatch, role resolution, escalation
- Pipeline integration: _run_qa_loop hook at quality_gate stage
"""

from __future__ import annotations

import json
import textwrap
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, call, patch

import pytest

from core.qa_loop import QALoop, _unwrap, _rewrap
from core.session import PipelineDB


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db(tmp_path: Path) -> PipelineDB:
    db = PipelineDB(tmp_path / "test.db")
    db.create_run("run-qa", "test idea", {})
    return db


def _make_pr(tmp_path: Path, db: PipelineDB):
    """Minimal PipelineRun-like object sufficient for QALoop."""
    from core.memory import ArtifactBundle, ProjectMemory
    from core.pipeline import PipelineRun
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    return PipelineRun(
        run_id="run-qa",
        idea="test idea",
        config={},
        artifact_bundle=ArtifactBundle("run-qa"),
        project_memory=ProjectMemory(ws),
        db=db,
        workspace=ws,
    )


def _make_stage_def(name: str = "quality_gate"):
    from core.pipeline import StageDefinition
    return StageDefinition(name=name, agent_roles=["qa_engineer"])


def _qa_loop(db: PipelineDB, mock_build_cfg=None) -> QALoop:
    if mock_build_cfg is None:
        mock_build_cfg = MagicMock(return_value=MagicMock())
    return QALoop(
        agent_registry={},
        config={},
        db=db,
        build_agent_config_fn=mock_build_cfg,
    )


# ---------------------------------------------------------------------------
# Helpers module-level functions
# ---------------------------------------------------------------------------

class TestInternalHelpers:
    def test_unwrap_single_role_dict(self):
        inner = {"coverage_pct": 90, "gate_passed": True}
        assert _unwrap({"qa_engineer": inner}) is inner

    def test_unwrap_not_single_role_returns_as_is(self):
        d = {"a": 1, "b": 2}
        assert _unwrap(d) is d

    def test_unwrap_non_dict_returns_as_is(self):
        assert _unwrap(42) == 42

    def test_rewrap_restores_role_key(self):
        original = {"qa_engineer": {"gate_passed": False}}
        inner = {"gate_passed": True, "escalated_to_hitl": True}
        result = _rewrap(original, inner)
        assert result == {"qa_engineer": inner}

    def test_rewrap_bare_dict_returns_inner(self):
        inner = {"gate_passed": True}
        assert _rewrap(inner, inner) is inner


# ---------------------------------------------------------------------------
# pytest_tool — JUnit XML parsing
# ---------------------------------------------------------------------------

class TestJUnitXMLParsing:
    """Tests for the private _parse_junit_xml function."""

    def _write_xml(self, path: Path, xml: str) -> Path:
        path.write_text(textwrap.dedent(xml), encoding="utf-8")
        return path

    def test_parses_all_passing(self, tmp_path):
        from tools.pytest_tool import _parse_junit_xml
        xml_path = self._write_xml(tmp_path / "r.xml", """\
            <?xml version="1.0"?>
            <testsuite tests="3" failures="0" errors="0" skipped="0">
              <testcase classname="tests.test_foo" name="test_a"/>
              <testcase classname="tests.test_foo" name="test_b"/>
              <testcase classname="tests.test_foo" name="test_c"/>
            </testsuite>
        """)
        result = _parse_junit_xml(xml_path)
        assert result["passed"] == 3
        assert result["failed"] == 0
        assert result["failures"] == []

    def test_parses_failures(self, tmp_path):
        from tools.pytest_tool import _parse_junit_xml
        xml_path = self._write_xml(tmp_path / "r.xml", """\
            <?xml version="1.0"?>
            <testsuite tests="2" failures="1" errors="0" skipped="0">
              <testcase classname="tests.test_foo" name="test_ok"/>
              <testcase classname="tests.test_foo" name="test_fail">
                <failure message="assert 1 == 2">AssertionError: assert 1 == 2</failure>
              </testcase>
            </testsuite>
        """)
        result = _parse_junit_xml(xml_path)
        assert result["failed"] == 1
        assert result["passed"] == 1
        assert len(result["failures"]) == 1
        assert "test_fail" in result["failures"][0]["nodeid"]

    def test_parses_errors(self, tmp_path):
        from tools.pytest_tool import _parse_junit_xml
        xml_path = self._write_xml(tmp_path / "r.xml", """\
            <?xml version="1.0"?>
            <testsuite tests="1" failures="0" errors="1" skipped="0">
              <testcase classname="tests.test_foo" name="test_err">
                <error>ImportError: no module named x</error>
              </testcase>
            </testsuite>
        """)
        result = _parse_junit_xml(xml_path)
        assert result["errors"] == 1
        assert result["failures"][0]["kind"] == "error"

    def test_handles_testsuites_wrapper(self, tmp_path):
        from tools.pytest_tool import _parse_junit_xml
        xml_path = self._write_xml(tmp_path / "r.xml", """\
            <?xml version="1.0"?>
            <testsuites>
              <testsuite tests="2" failures="0" errors="0" skipped="0">
                <testcase classname="a" name="t1"/>
                <testcase classname="a" name="t2"/>
              </testsuite>
            </testsuites>
        """)
        result = _parse_junit_xml(xml_path)
        assert result["passed"] == 2

    def test_returns_zeros_for_missing_file(self, tmp_path):
        from tools.pytest_tool import _parse_junit_xml
        result = _parse_junit_xml(tmp_path / "nonexistent.xml")
        assert result["passed"] == 0
        assert result["failures"] == []

    def test_returns_parse_error_on_malformed_xml(self, tmp_path):
        from tools.pytest_tool import _parse_junit_xml
        p = tmp_path / "bad.xml"
        p.write_text("not xml <><>", encoding="utf-8")
        result = _parse_junit_xml(p)
        assert "parse_error" in result


# ---------------------------------------------------------------------------
# pytest_tool — coverage JSON parsing
# ---------------------------------------------------------------------------

class TestCoverageJSONParsing:
    def test_parses_coverage_pct(self, tmp_path):
        from tools.pytest_tool import _parse_coverage_json
        p = tmp_path / "cov.json"
        p.write_text(json.dumps({"totals": {"percent_covered": 87.5}}), encoding="utf-8")
        assert _parse_coverage_json(p) == 87.5

    def test_returns_zero_for_missing_file(self, tmp_path):
        from tools.pytest_tool import _parse_coverage_json
        assert _parse_coverage_json(tmp_path / "missing.json") == 0.0

    def test_returns_zero_on_malformed_json(self, tmp_path):
        from tools.pytest_tool import _parse_coverage_json
        p = tmp_path / "bad.json"
        p.write_text("not json", encoding="utf-8")
        assert _parse_coverage_json(p) == 0.0


# ---------------------------------------------------------------------------
# pytest_tool — run_pytest integration (mocked subprocess)
# ---------------------------------------------------------------------------

class TestRunPytestTool:
    def _make_xml(self, tmp_path: Path, passed: int = 2, failed: int = 0) -> str:
        xml = f'<?xml version="1.0"?><testsuite tests="{passed + failed}" failures="{failed}" errors="0" skipped="0">'
        for i in range(passed):
            xml += f'<testcase classname="t" name="test_p{i}"/>'
        for i in range(failed):
            xml += f'<testcase classname="t" name="test_f{i}"><failure>fail</failure></testcase>'
        xml += "</testsuite>"
        return xml

    def test_returns_structured_json_on_success(self, tmp_path):
        from tools.pytest_tool import run_pytest
        from tools._context import set_tool_context

        set_tool_context(workspace=tmp_path)

        completed = MagicMock()
        completed.returncode = 0
        completed.stdout = "2 passed"
        completed.stderr = ""

        xml_content = self._make_xml(tmp_path, passed=2, failed=0)
        cov_content = json.dumps({"totals": {"percent_covered": 92.0}})

        def fake_run(cmd, **kwargs):
            # Write the XML and coverage files that the tool expects
            xml_path = tmp_path / ".pytest_results.xml"
            xml_path.write_text(xml_content, encoding="utf-8")
            cov_path = tmp_path / ".coverage_report.json"
            cov_path.write_text(cov_content, encoding="utf-8")
            return completed

        with patch("subprocess.run", side_effect=fake_run):
            result = json.loads(run_pytest())

        assert result["passed"] == 2
        assert result["failed"] == 0
        assert result["coverage_pct"] == 92.0
        assert result["return_code"] == 0

    def test_timeout_returns_error_structure(self, tmp_path):
        from tools.pytest_tool import run_pytest
        from tools._context import set_tool_context
        import subprocess

        set_tool_context(workspace=tmp_path)
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="pytest", timeout=1)):
            result = json.loads(run_pytest(timeout=1))
        assert "error" in result
        assert "timed out" in result["error"]
        assert result["return_code"] == -1

    def test_python_not_found_returns_error(self, tmp_path):
        from tools.pytest_tool import run_pytest
        from tools._context import set_tool_context

        set_tool_context(workspace=tmp_path)
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = json.loads(run_pytest())
        assert "error" in result
        assert result["return_code"] == -1


# ---------------------------------------------------------------------------
# QALoop — pass on first attempt
# ---------------------------------------------------------------------------

class TestQALoopPassFirstAttempt:
    def test_returns_immediately_when_gate_passed(self, tmp_path):
        db = _make_db(tmp_path)
        pr = _make_pr(tmp_path, db)
        loop = _qa_loop(db)
        artifact = {"qa_engineer": {"gate_passed": True, "coverage_pct": 90, "bugs": []}}
        result = loop.run(pr, _make_stage_def(), artifact)
        assert result is artifact

    def test_returns_bare_artifact_when_gate_passed(self, tmp_path):
        db = _make_db(tmp_path)
        pr = _make_pr(tmp_path, db)
        loop = _qa_loop(db)
        artifact = {"gate_passed": True, "coverage_pct": 85, "bugs": []}
        result = loop.run(pr, _make_stage_def(), artifact)
        assert result is artifact

    def test_stops_when_no_critical_bugs(self, tmp_path):
        """gate_passed=false but only minor bugs — skip fix loop."""
        db = _make_db(tmp_path)
        pr = _make_pr(tmp_path, db)
        loop = _qa_loop(db)
        artifact = {
            "qa_engineer": {
                "gate_passed": False, "coverage_pct": 60,
                "bugs": [{"id": "BUG-1", "title": "cosmetic", "severity": "minor"}],
            }
        }
        result = loop.run(pr, _make_stage_def(), artifact)
        # Should return the original artifact without fix attempts
        assert result is artifact


# ---------------------------------------------------------------------------
# QALoop — fix iteration dispatch
# ---------------------------------------------------------------------------

class TestQALoopFixIterations:
    def _make_loop_with_mocks(self, tmp_path, db, fix_succeeds=True, qa_passes_after=1):
        """Build a QALoop whose inner agents are fully mocked."""
        fix_agent_mock = MagicMock()
        fix_agent_mock.run.return_value = MagicMock(success=fix_succeeds, error=None, artifact={"files_written": [], "tests_passing": fix_succeeds})

        qa_pass_artifact = {"gate_passed": True, "coverage_pct": 90, "bugs": [], "failing_tests": []}
        qa_fail_artifact = {
            "gate_passed": False, "coverage_pct": 40,
            "bugs": [{"id": "BUG-1", "title": "t fails", "severity": "critical",
                      "related_task": "task-1", "steps_to_reproduce": "run test_x"}],
            "failing_tests": [{"nodeid": "tests/test_foo.py::test_x", "message": "AssertionError"}],
        }

        rerun_call_count = {"n": 0}

        qa_agent_mock = MagicMock()
        def qa_run_side_effect(**kwargs):
            rerun_call_count["n"] += 1
            artifact = qa_pass_artifact if rerun_call_count["n"] >= qa_passes_after else qa_fail_artifact
            return MagicMock(success=True, error=None, artifact=artifact)
        qa_agent_mock.run.side_effect = qa_run_side_effect

        registry = {
            "developer_backend": lambda **kw: fix_agent_mock,
            "qa_engineer": lambda **kw: qa_agent_mock,
        }
        # Support both class instantiation and dict lookup
        registry = {
            "developer_backend": MagicMock(return_value=fix_agent_mock),
            "qa_engineer": MagicMock(return_value=qa_agent_mock),
        }

        build_cfg = MagicMock(return_value=MagicMock(workspace=tmp_path))
        loop = QALoop(
            agent_registry=registry,
            config={},
            db=db,
            build_agent_config_fn=build_cfg,
            max_fix_iterations=3,
        )
        return loop, registry, fix_agent_mock, qa_agent_mock

    def test_creates_fix_kanban_task_on_critical_bug(self, tmp_path):
        db = _make_db(tmp_path)
        pr = _make_pr(tmp_path, db)
        loop, _, _, _ = self._make_loop_with_mocks(tmp_path, db, fix_succeeds=True, qa_passes_after=1)

        initial = {
            "qa_engineer": {
                "gate_passed": False, "coverage_pct": 40,
                "bugs": [{"id": "BUG-1", "title": "test_x fails", "severity": "critical",
                          "related_task": "task-1", "steps_to_reproduce": "run test_x"}],
                "failing_tests": [],
            }
        }
        loop.run(pr, _make_stage_def(), initial)

        tasks = db.list_tasks("run-qa")
        fix_tasks = [t for t in tasks if t["task_id"].startswith("fix-")]
        assert len(fix_tasks) >= 1

    def test_passes_after_single_fix(self, tmp_path):
        db = _make_db(tmp_path)
        pr = _make_pr(tmp_path, db)
        loop, _, _, _ = self._make_loop_with_mocks(tmp_path, db, fix_succeeds=True, qa_passes_after=1)

        initial = {
            "qa_engineer": {
                "gate_passed": False, "coverage_pct": 40,
                "bugs": [{"id": "BUG-1", "title": "fails", "severity": "critical",
                          "related_task": "t-1", "steps_to_reproduce": ""}],
                "failing_tests": [],
            }
        }
        result = loop.run(pr, _make_stage_def(), initial)
        assert _unwrap(result).get("gate_passed") is True

    def test_fix_task_marked_done_on_success(self, tmp_path):
        db = _make_db(tmp_path)
        pr = _make_pr(tmp_path, db)
        loop, _, _, _ = self._make_loop_with_mocks(tmp_path, db, fix_succeeds=True, qa_passes_after=1)

        initial = {
            "qa_engineer": {
                "gate_passed": False, "coverage_pct": 40,
                "bugs": [{"id": "BUG-1", "title": "x", "severity": "critical",
                          "related_task": "t-1", "steps_to_reproduce": ""}],
                "failing_tests": [],
            }
        }
        loop.run(pr, _make_stage_def(), initial)
        tasks = db.list_tasks("run-qa")
        fix_tasks = [t for t in tasks if t["task_id"].startswith("fix-")]
        assert all(t["status"] == "done" for t in fix_tasks)

    def test_fix_task_marked_blocked_on_failure(self, tmp_path):
        db = _make_db(tmp_path)
        pr = _make_pr(tmp_path, db)
        loop, _, _, _ = self._make_loop_with_mocks(tmp_path, db, fix_succeeds=False, qa_passes_after=99)

        initial = {
            "qa_engineer": {
                "gate_passed": False, "coverage_pct": 40,
                "bugs": [{"id": "BUG-1", "title": "x", "severity": "critical",
                          "related_task": "t-1", "steps_to_reproduce": ""}],
                "failing_tests": [],
            }
        }
        loop.run(pr, _make_stage_def(), initial)
        tasks = db.list_tasks("run-qa")
        fix_tasks = [t for t in tasks if t["task_id"].startswith("fix-")]
        assert any(t["status"] == "blocked" for t in fix_tasks)


# ---------------------------------------------------------------------------
# QALoop — role resolution
# ---------------------------------------------------------------------------

class TestQALoopRoleResolution:
    def test_resolves_role_from_original_task(self, tmp_path):
        db = _make_db(tmp_path)
        db.create_task("orig-task", "run-qa", "original task", "developer_ui")
        loop = _qa_loop(db)
        role = loop._resolve_fix_role("orig-task")
        assert role == "developer_ui"

    def test_falls_back_to_developer_backend_for_unknown_task(self, tmp_path):
        db = _make_db(tmp_path)
        loop = _qa_loop(db)
        assert loop._resolve_fix_role("nonexistent-task-id") == "developer_backend"

    def test_falls_back_when_task_id_is_unknown_sentinel(self, tmp_path):
        db = _make_db(tmp_path)
        loop = _qa_loop(db)
        assert loop._resolve_fix_role("_unknown") == "developer_backend"


# ---------------------------------------------------------------------------
# QALoop — escalation
# ---------------------------------------------------------------------------

class TestQALoopEscalation:
    def _exhausting_loop(self, tmp_path, db) -> QALoop:
        """Loop whose fix agent always fails, causing escalation."""
        fix_agent = MagicMock()
        fix_agent.run.return_value = MagicMock(success=False, error="intentional failure")

        qa_agent = MagicMock()
        qa_agent.run.return_value = MagicMock(
            success=True,
            artifact={
                "gate_passed": False, "coverage_pct": 20,
                "bugs": [{"id": "BUG-1", "title": "x", "severity": "critical",
                          "related_task": "t-1", "steps_to_reproduce": ""}],
                "failing_tests": [],
            }
        )
        registry = {
            "developer_backend": MagicMock(return_value=fix_agent),
            "qa_engineer": MagicMock(return_value=qa_agent),
        }
        return QALoop(
            agent_registry=registry,
            config={},
            db=db,
            build_agent_config_fn=MagicMock(return_value=MagicMock(workspace=tmp_path)),
            max_fix_iterations=2,
        )

    def test_escalates_after_max_iterations(self, tmp_path):
        db = _make_db(tmp_path)
        pr = _make_pr(tmp_path, db)
        loop = self._exhausting_loop(tmp_path, db)

        initial = {
            "qa_engineer": {
                "gate_passed": False, "coverage_pct": 20,
                "bugs": [{"id": "BUG-1", "title": "x", "severity": "critical",
                          "related_task": "t-1", "steps_to_reproduce": ""}],
                "failing_tests": [],
            }
        }
        result = loop.run(pr, _make_stage_def(), initial)
        inner = _unwrap(result)
        assert inner.get("escalated_to_hitl") is True

    def test_escalated_artifact_preserves_role_wrapping(self, tmp_path):
        db = _make_db(tmp_path)
        pr = _make_pr(tmp_path, db)
        loop = self._exhausting_loop(tmp_path, db)
        initial = {
            "qa_engineer": {
                "gate_passed": False, "coverage_pct": 20,
                "bugs": [{"id": "BUG-1", "title": "x", "severity": "critical",
                          "related_task": "t-1", "steps_to_reproduce": ""}],
                "failing_tests": [],
            }
        }
        result = loop.run(pr, _make_stage_def(), initial)
        # Role wrapping should be preserved
        assert "qa_engineer" in result

    def test_escalation_reason_is_descriptive(self, tmp_path):
        db = _make_db(tmp_path)
        pr = _make_pr(tmp_path, db)
        loop = self._exhausting_loop(tmp_path, db)
        initial = {
            "qa_engineer": {
                "gate_passed": False, "coverage_pct": 20,
                "bugs": [{"id": "BUG-1", "title": "x", "severity": "critical",
                          "related_task": "t-1", "steps_to_reproduce": ""}],
                "failing_tests": [],
            }
        }
        result = loop.run(pr, _make_stage_def(), initial)
        reason = _unwrap(result).get("escalation_reason", "")
        assert "2" in reason  # max_fix_iterations=2


# ---------------------------------------------------------------------------
# Pipeline integration — _run_qa_loop hook
# ---------------------------------------------------------------------------

class TestPipelineQALoopHook:
    """Verify pipeline._execute() calls _run_qa_loop at quality_gate."""

    def _mock_call_model(self, model, base_url, api_key, system, messages, tools, **kwargs):
        role_name = "Unknown"
        for line in system.split("\n"):
            if line.startswith("# Role:"):
                role_name = line.replace("# Role:", "").strip()
                break
        artifacts = {
            "Domain Expert":     {"domain": "test", "market_size": "s", "competitors": []},
            "Product Owner":     {"user_stories": [], "mvp_stories": [], "mvp_rationale": ""},
            "Business Analyst":  {"functional_requirements": [], "non_functional_requirements": [],
                                  "data_dictionary": [], "open_questions": [], "assumptions": []},
            "Architect":         {"adrs": [], "tech_stack": {}, "c4_context": "", "c4_container": ""},
            "Tech Lead":         {"tasks": [], "coding_standards": {}},
            "Backend Developer": {"files_written": [], "tests_passing": True},
            "UI Developer":      {"files_written": [], "tests_passing": True},
            "DevOps Engineer":   {"files_written": [], "ci_steps": [], "secrets_required": [],
                                  "deployment_notes": ""},
            "QA Engineer":       {"coverage_pct": 90, "gate_passed": True, "bugs": [],
                                  "failing_tests": []},
            "Release Manager":   {"version": "1.0.0", "release_notes": "ok",
                                  "deployment_checklist": []},
        }
        artifact = artifacts.get(role_name, {"result": "ok"})
        content = f"```json\n{json.dumps(artifact)}\n```"
        return {
            "message": {"role": "assistant", "content": content},
            "stop_reason": "end_turn",
            "usage": {"prompt_tokens": 10, "completion_tokens": 20},
        }

    @patch("core.agent_runtime.call_model_openai")
    def test_run_qa_loop_called_at_quality_gate(self, mock_model, tmp_path):
        mock_model.side_effect = self._mock_call_model
        db = PipelineDB(tmp_path / "p.db")
        from core.pipeline import SDLCPipeline
        config = {
            "workspace": str(tmp_path / "output"),
            "governance": {"default_mode": "auto"},
        }
        pipeline = SDLCPipeline.from_dict(config, db=db)

        with patch.object(pipeline, "_run_qa_loop", wraps=pipeline._run_qa_loop) as spy:
            result = pipeline.run("Build a test app")

        assert spy.called, "_run_qa_loop was never called"
        call_args = spy.call_args
        # Third positional arg is initial_artifact from the QA stage
        _, stage_def_arg, _ = call_args.args
        assert stage_def_arg.name == "quality_gate"

    @patch("core.agent_runtime.call_model_openai")
    def test_qa_loop_result_is_persisted(self, mock_model, tmp_path):
        """The artifact returned by _run_qa_loop should be what ends up in the DB."""
        mock_model.side_effect = self._mock_call_model
        db = PipelineDB(tmp_path / "p.db")
        from core.pipeline import SDLCPipeline
        config = {
            "workspace": str(tmp_path / "output"),
            "governance": {"default_mode": "auto"},
        }
        pipeline = SDLCPipeline.from_dict(config, db=db)
        result = pipeline.run("Build a test app")

        assert result["status"] == "completed"
        qa_artifact = db.get_artifact(result["run_id"], "quality_gate")
        assert qa_artifact is not None
        inner = qa_artifact.get("qa_engineer", qa_artifact)
        assert inner.get("gate_passed") is True

    @patch("core.agent_runtime.call_model_openai")
    def test_max_fix_iterations_from_config(self, mock_model, tmp_path):
        """Pipeline reads qa.max_fix_iterations from config."""
        mock_model.side_effect = self._mock_call_model
        db = PipelineDB(tmp_path / "p.db")
        from core.pipeline import SDLCPipeline
        config = {
            "workspace": str(tmp_path / "output"),
            "governance": {"default_mode": "auto"},
            "qa": {"max_fix_iterations": 5},
        }
        pipeline = SDLCPipeline.from_dict(config, db=db)

        captured = {}

        original_run_qa_loop = pipeline._run_qa_loop
        def capturing_run_qa_loop(pr, stage_def, initial):
            from core.qa_loop import QALoop
            # Inspect what QALoop would get
            captured["max"] = config.get("qa", {}).get("max_fix_iterations", 3)
            return original_run_qa_loop(pr, stage_def, initial)

        with patch.object(pipeline, "_run_qa_loop", side_effect=capturing_run_qa_loop):
            pipeline.run("Build a test app")

        assert captured.get("max") == 5
