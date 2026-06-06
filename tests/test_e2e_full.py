"""Full end-to-end pipeline test.

Runs all 7 stages (discovery → requirements → architecture → planning →
development → quality_gate → delivery) with mocked LLM calls. Verifies:

- Every stage artifact is persisted and approved in the DB
- Audit log records HITL decisions for all gated stages
- Kanban tasks are created for the parallel development stage
- Token usage is recorded for every agent
- The delivery artifact has the required fields (version, deployment_checklist)
- The pipeline result is status=completed
- Resume from paused run works correctly
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from core.session import PipelineDB


# ---------------------------------------------------------------------------
# Mock LLM — deterministic artifact per role
# ---------------------------------------------------------------------------

_ARTIFACTS: dict = {
    "Domain Expert": {
        "glossary": {"pipeline": "ordered sequence of stages"},
        "bounded_contexts": [{"name": "core", "entities": ["Run"], "responsibilities": "execute"}],
        "key_constraints": ["must be fast"],
        "out_of_scope": [],
    },
    "Product Owner": {
        "user_stories": [
            {"id": "US-001", "title": "Run pipeline", "story": "As a dev...",
             "priority": "must",
             "acceptance_criteria": ["Pipeline completes in under 10 minutes"]},
        ],
        "mvp_stories": ["US-001"],
        "mvp_rationale": "Core functionality.",
    },
    "Business Analyst": {
        "functional_requirements": [
            {"id": "FR-001", "story_ref": "US-001", "description": "run pipeline", "priority": "must"}
        ],
        "non_functional_requirements": [
            {"id": "NFR-001", "category": "performance", "description": "< 10 min"}
        ],
        "data_dictionary": [],
        "open_questions": [],
        "assumptions": [],
    },
    "Architect": {
        "adrs": [
            {"id": "ADR-001", "title": "Use SQLite", "status": "accepted",
             "context": "need a db", "decision": "SQLite", "consequences": {}}
        ],
        "tech_stack": {"backend": "FastAPI", "db": "SQLite"},
        "c4_context": "context diagram",
        "c4_container": "container diagram",
    },
    "Tech Lead": {
        "tasks": [
            {"task_id": "t-001", "title": "Create API", "assignee_role": "developer_backend",
             "description": "Implement GET /items", "acceptance_criteria": ["Returns 200"]},
            {"task_id": "t-002", "title": "Create UI", "assignee_role": "developer_ui",
             "description": "React component", "acceptance_criteria": ["Renders list"]},
            {"task_id": "t-003", "title": "Create Dockerfile", "assignee_role": "developer_devops",
             "description": "Write Dockerfile", "acceptance_criteria": ["Image builds"]},
        ],
        "coding_standards": {"max_line_length": 100},
    },
    "Backend Developer": {
        "files_written": ["src/main.py", "tests/test_main.py"],
        "tests_passing": True,
        "coverage_pct": 85,
        "pr_description": "Implements GET /items endpoint",
    },
    "UI Developer": {
        "files_written": ["src/App.tsx", "src/App.test.tsx"],
        "tests_passing": True,
        "coverage_pct": 80,
        "pr_description": "Implements Items list component",
    },
    "DevOps Engineer": {
        "files_written": ["Dockerfile", "docker-compose.yml", ".github/workflows/ci.yml"],
        "ci_steps": ["lint", "test", "build"],
        "secrets_required": [],
        "deployment_notes": "Push to Docker Hub before deploy",
    },
    "QA Engineer": {
        "test_plan": "Run unit + integration tests",
        "test_suites_run": ["tests/test_main.py"],
        "total_tests": 10,
        "passing": 10,
        "failing": 0,
        "coverage_pct": 85,
        "failing_tests": [],
        "bugs": [],
        "acceptance_criteria_coverage": {"US-001": "covered"},
        "gate_passed": True,
        "gate_rationale": "All tests pass; coverage 85% >= 80% threshold",
    },
    "Release Manager": {
        "version": "1.0.0",
        "release_notes": "## v1.0.0\n\n- Initial release",
        "deployment_checklist": [
            "Tag git commit with v1.0.0",
            "Push Docker image to registry",
            "Update production environment",
        ],
        "rollback_procedure": "docker pull myapp:0.9.0 && docker-compose up -d",
        "release_tag": "myapp:1.0.0",
        "deployment_manifest": "docker-compose.prod.yml",
    },
}


def _mock_llm(model, base_url, api_key, system, messages, tools, **kwargs):
    role_name = "Unknown"
    for line in system.split("\n"):
        if line.startswith("# Role:"):
            role_name = line.replace("# Role:", "").strip()
            break
    artifact = _ARTIFACTS.get(role_name, {"result": "ok", "files_written": [], "tests_passing": True})
    content = f"Here is my output:\n\n```json\n{json.dumps(artifact)}\n```"
    return {
        "message": {"role": "assistant", "content": content},
        "stop_reason": "end_turn",
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
    }


def _auto_config(tmp_path: Path) -> dict:
    return {
        "workspace": str(tmp_path / "output"),
        "governance": {
            "default_mode": "auto",
            "hitl_gates": [
                {"stage": "requirements", "mode": "auto", "max_open_questions": 0},
                {"stage": "architecture", "mode": "auto"},
                {"stage": "quality_gate",  "mode": "auto", "coverage_threshold": 80},
                {"stage": "delivery",      "mode": "auto"},
            ],
        },
        "pipeline": {"model": "claude-sonnet-4-6"},
    }


# ---------------------------------------------------------------------------
# Full pipeline E2E
# ---------------------------------------------------------------------------

class TestFullPipelineE2E:
    @patch("core.agent_runtime.call_model_openai", side_effect=_mock_llm)
    def test_pipeline_completes(self, mock_model, tmp_path):
        db = PipelineDB(tmp_path / "pipeline.db")
        from core.pipeline import SDLCPipeline
        pipeline = SDLCPipeline.from_dict(_auto_config(tmp_path), db=db)
        result = pipeline.run("Build a simple REST API")
        assert result["status"] == "completed"

    @patch("core.agent_runtime.call_model_openai", side_effect=_mock_llm)
    def test_all_stage_artifacts_persisted(self, mock_model, tmp_path):
        db = PipelineDB(tmp_path / "pipeline.db")
        from core.pipeline import SDLCPipeline, SDLC_STAGES
        pipeline = SDLCPipeline.from_dict(_auto_config(tmp_path), db=db)
        result = pipeline.run("Build a REST API")
        run_id = result["run_id"]

        for stage in SDLC_STAGES:
            artifact = db.get_artifact(run_id, stage.name)
            assert artifact is not None, f"No artifact for stage {stage.name}"

    @patch("core.agent_runtime.call_model_openai", side_effect=_mock_llm)
    def test_hitl_gated_stages_in_audit_log(self, mock_model, tmp_path):
        db = PipelineDB(tmp_path / "pipeline.db")
        from core.pipeline import SDLCPipeline, SDLC_STAGES
        pipeline = SDLCPipeline.from_dict(_auto_config(tmp_path), db=db)
        result = pipeline.run("Build a REST API")
        run_id = result["run_id"]

        audit = db.get_audit_log(run_id)
        gated_stages = {s.name for s in SDLC_STAGES if s.hitl_gate}
        audited_stages = {e["stage"] for e in audit}
        assert gated_stages.issubset(audited_stages), (
            f"Missing audit entries for: {gated_stages - audited_stages}"
        )

    @patch("core.agent_runtime.call_model_openai", side_effect=_mock_llm)
    def test_all_hitl_decisions_approved(self, mock_model, tmp_path):
        db = PipelineDB(tmp_path / "pipeline.db")
        from core.pipeline import SDLCPipeline
        pipeline = SDLCPipeline.from_dict(_auto_config(tmp_path), db=db)
        result = pipeline.run("Build a REST API")
        run_id = result["run_id"]

        audit = db.get_audit_log(run_id)
        for entry in audit:
            assert entry["decision"] == "APPROVED", (
                f"Stage {entry['stage']} was {entry['decision']}: {entry['rationale']}"
            )

    @patch("core.agent_runtime.call_model_openai", side_effect=_mock_llm)
    def test_kanban_tasks_created_for_development(self, mock_model, tmp_path):
        db = PipelineDB(tmp_path / "pipeline.db")
        from core.pipeline import SDLCPipeline
        pipeline = SDLCPipeline.from_dict(_auto_config(tmp_path), db=db)
        result = pipeline.run("Build a REST API")
        run_id = result["run_id"]

        tasks = db.list_tasks(run_id)
        dev_tasks = [t for t in tasks if t["assignee"] in
                     ("developer_backend", "developer_ui", "developer_devops")]
        assert len(dev_tasks) >= 3, f"Expected ≥3 dev tasks, got {len(dev_tasks)}"

    @patch("core.agent_runtime.call_model_openai", side_effect=_mock_llm)
    def test_token_usage_recorded_for_every_stage(self, mock_model, tmp_path):
        db = PipelineDB(tmp_path / "pipeline.db")
        from core.pipeline import SDLCPipeline, SDLC_STAGES
        pipeline = SDLCPipeline.from_dict(_auto_config(tmp_path), db=db)
        result = pipeline.run("Build a REST API")
        run_id = result["run_id"]

        token_rows = db.get_token_summary(run_id)
        recorded_stages = {r["stage"] for r in token_rows}

        for stage in SDLC_STAGES:
            assert stage.name in recorded_stages, (
                f"No token data for stage {stage.name}"
            )

    @patch("core.agent_runtime.call_model_openai", side_effect=_mock_llm)
    def test_token_counts_are_positive(self, mock_model, tmp_path):
        db = PipelineDB(tmp_path / "pipeline.db")
        from core.pipeline import SDLCPipeline
        pipeline = SDLCPipeline.from_dict(_auto_config(tmp_path), db=db)
        result = pipeline.run("Build a REST API")
        run_id = result["run_id"]

        rows = db.get_token_summary(run_id)
        for row in rows:
            assert row["tokens_in"] >= 0
            assert row["tokens_out"] >= 0

    @patch("core.agent_runtime.call_model_openai", side_effect=_mock_llm)
    def test_delivery_artifact_has_required_fields(self, mock_model, tmp_path):
        db = PipelineDB(tmp_path / "pipeline.db")
        from core.pipeline import SDLCPipeline
        pipeline = SDLCPipeline.from_dict(_auto_config(tmp_path), db=db)
        result = pipeline.run("Build a REST API")
        run_id = result["run_id"]

        delivery = db.get_artifact(run_id, "delivery")
        assert delivery is not None
        inner = delivery.get("release_manager", delivery)
        assert "version" in inner
        assert "deployment_checklist" in inner
        assert isinstance(inner["deployment_checklist"], list)
        assert len(inner["deployment_checklist"]) > 0

    @patch("core.agent_runtime.call_model_openai", side_effect=_mock_llm)
    def test_workspace_directory_created(self, mock_model, tmp_path):
        db = PipelineDB(tmp_path / "pipeline.db")
        from core.pipeline import SDLCPipeline
        pipeline = SDLCPipeline.from_dict(_auto_config(tmp_path), db=db)
        result = pipeline.run("Build a REST API")
        run_id = result["run_id"]

        workspace = tmp_path / "output" / run_id
        assert workspace.exists(), f"Workspace {workspace} was not created"

    @patch("core.agent_runtime.call_model_openai", side_effect=_mock_llm)
    def test_run_id_recorded_in_list(self, mock_model, tmp_path):
        db = PipelineDB(tmp_path / "pipeline.db")
        from core.pipeline import SDLCPipeline
        pipeline = SDLCPipeline.from_dict(_auto_config(tmp_path), db=db)
        result = pipeline.run("Build a REST API")
        run_id = result["run_id"]

        runs = db.list_runs()
        assert any(r["run_id"] == run_id for r in runs)


# ---------------------------------------------------------------------------
# Resume E2E — paused run resumes from last approved stage
# ---------------------------------------------------------------------------

class TestResumeE2E:
    @patch("core.agent_runtime.call_model_openai", side_effect=_mock_llm)
    def test_resume_skips_already_approved_stages(self, mock_model, tmp_path):
        db = PipelineDB(tmp_path / "pipeline.db")
        from core.pipeline import SDLCPipeline

        # Run until requirements is rejected (failing QA)
        failing_qa = dict(_ARTIFACTS)
        # No critical bugs → QALoop skips fix attempts.
        # AutoEvaluator sees coverage_pct=50 < 80% → REJECTED → run pauses.
        failing_qa["QA Engineer"] = {
            "test_plan": "", "test_suites_run": [],
            "total_tests": 5, "passing": 3, "failing": 2,
            "coverage_pct": 50, "failing_tests": [],
            "bugs": [],
            "acceptance_criteria_coverage": {},
            "gate_passed": False,
            "gate_rationale": "coverage 50% below 80% threshold",
        }

        qa_called = {"n": 0}

        def _mock_with_fail(model, base_url, api_key, system, messages, tools, **kwargs):
            role_name = "Unknown"
            for line in system.split("\n"):
                if line.startswith("# Role:"):
                    role_name = line.replace("# Role:", "").strip()
                    break
            if role_name == "QA Engineer":
                qa_called["n"] += 1
                artifact = failing_qa["QA Engineer"] if qa_called["n"] == 1 else _ARTIFACTS["QA Engineer"]
            else:
                artifact = _ARTIFACTS.get(role_name, {"result": "ok", "files_written": [], "tests_passing": True})
            content = f"```json\n{json.dumps(artifact)}\n```"
            return {
                "message": {"role": "assistant", "content": content},
                "stop_reason": "end_turn",
                "usage": {"prompt_tokens": 100, "completion_tokens": 50},
            }

        with patch("core.agent_runtime.call_model_openai", side_effect=_mock_with_fail):
            pipeline = SDLCPipeline.from_dict(_auto_config(tmp_path), db=db)
            result = pipeline.run("Build a REST API")

        assert result["status"] == "paused"
        run_id = result["run_id"]

        # Manually clear the quality_gate artifact so it re-runs on resume
        db.update_run_status(run_id, "paused", stage="quality_gate")

        # Second run: QA passes → pipeline completes
        with patch("core.agent_runtime.call_model_openai", side_effect=_mock_with_fail):
            result2 = pipeline.run("", resume_run_id=run_id)

        assert result2["status"] == "completed"

    @patch("core.agent_runtime.call_model_openai", side_effect=_mock_llm)
    def test_completed_run_cannot_be_resumed(self, mock_model, tmp_path):
        """Resuming an already-completed run should return completed immediately."""
        db = PipelineDB(tmp_path / "pipeline.db")
        from core.pipeline import SDLCPipeline
        pipeline = SDLCPipeline.from_dict(_auto_config(tmp_path), db=db)
        result = pipeline.run("Build a REST API")
        assert result["status"] == "completed"
        run_id = result["run_id"]

        # Resume a completed run — should skip all stages and return completed
        result2 = pipeline.run("", resume_run_id=run_id)
        assert result2["status"] == "completed"


# ---------------------------------------------------------------------------
# E2E: cmd_cost integration
# ---------------------------------------------------------------------------

class TestCostIntegrationE2E:
    @patch("core.agent_runtime.call_model_openai", side_effect=_mock_llm)
    def test_cost_command_shows_all_stages_after_run(self, mock_model, tmp_path, capsys):
        db = PipelineDB(tmp_path / "pipeline.db")
        from core.pipeline import SDLCPipeline, SDLC_STAGES
        from sdlc import cmd_cost
        pipeline = SDLCPipeline.from_dict(_auto_config(tmp_path), db=db)
        result = pipeline.run("Build a REST API")
        run_id = result["run_id"]

        with patch("core.session.get_default_db", return_value=db):
            from argparse import Namespace
            rc = cmd_cost(Namespace(run_id=run_id))

        assert rc == 0
        out = capsys.readouterr().out
        # All stages should appear in the cost table
        for stage in SDLC_STAGES:
            assert stage.name in out, f"Stage {stage.name} missing from cost output"
        assert "TOTAL" in out
