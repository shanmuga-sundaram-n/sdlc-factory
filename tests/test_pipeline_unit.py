"""Unit tests for core pipeline components.

Tests are intentionally isolated: no real model calls, no real file I/O.
Patches call_model_openai at the boundary so every other layer is tested real.
"""

import json
import threading
from unittest.mock import patch

import pytest

from core.memory import ArtifactBundle, ProjectMemory, StreamingContextScrubber, sanitize_context
from core.session import PipelineDB
from core.registry import ToolRegistry

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_workspace(tmp_path):
    return tmp_path / "workspace"


@pytest.fixture
def db(tmp_path):
    return PipelineDB(db_path=tmp_path / "test_pipeline.db")


@pytest.fixture
def project_memory(tmp_workspace):
    tmp_workspace.mkdir(parents=True, exist_ok=True)
    return ProjectMemory(tmp_workspace)


@pytest.fixture
def artifact_bundle():
    return ArtifactBundle(run_id="test-run-001")


# ---------------------------------------------------------------------------
# ArtifactBundle tests
# ---------------------------------------------------------------------------


class TestArtifactBundle:
    def test_set_and_get_stage(self, artifact_bundle):
        artifact_bundle.set_stage("discovery", {"glossary": {"user": "a person"}})
        result = artifact_bundle.get_stage("discovery")
        assert result == {"glossary": {"user": "a person"}}

    def test_get_missing_stage_returns_none(self, artifact_bundle):
        assert artifact_bundle.get_stage("nonexistent") is None

    def test_context_block_excludes_current_stage(self, artifact_bundle):
        artifact_bundle.set_stage("discovery", {"foo": "bar"})
        artifact_bundle.set_stage("requirements", {"brs": "..."})
        block = artifact_bundle.build_context_block(
            for_stage="architecture",
            prior_stages=["discovery"],
        )
        assert "discovery" in block
        assert "requirements" not in block

    def test_context_block_empty_when_no_prior(self, artifact_bundle):
        block = artifact_bundle.build_context_block("discovery", [])
        assert block == ""

    def test_from_dict_roundtrip(self, artifact_bundle):
        artifact_bundle.set_stage("discovery", {"x": 1})
        d = artifact_bundle.as_dict()
        restored = ArtifactBundle.from_dict(d)
        assert restored.get_stage("discovery") == {"x": 1}


# ---------------------------------------------------------------------------
# StreamingContextScrubber tests
# ---------------------------------------------------------------------------


class TestStreamingContextScrubber:
    def test_strips_complete_span(self):
        scrubber = StreamingContextScrubber()
        text = "hello <stage-context>secret</stage-context> world"
        out = scrubber.feed(text)
        assert "secret" not in out
        assert "hello" in out
        assert "world" in out

    def test_passes_through_plain_text(self):
        scrubber = StreamingContextScrubber()
        out = scrubber.feed("just normal text")
        assert out == "just normal text"

    def test_survives_split_open_tag(self):
        scrubber = StreamingContextScrubber()
        out1 = scrubber.feed("before <stage-")
        out2 = scrubber.feed("context>hidden</stage-context> after")
        combined = out1 + out2
        assert "hidden" not in combined
        assert "before" in combined
        assert "after" in combined

    def test_flush_emits_held_non_tag(self):
        scrubber = StreamingContextScrubber()
        scrubber.feed("partial <")
        tail = scrubber.flush()
        # The held "<" is not a real tag — flush should emit it
        assert "<" in tail

    def test_sanitize_context_strips_fences(self):
        text = "before <stage-context>secret data</stage-context> after"
        result = sanitize_context(text)
        assert "secret" not in result
        assert "before" in result
        assert "after" in result


# ---------------------------------------------------------------------------
# ProjectMemory tests
# ---------------------------------------------------------------------------


class TestProjectMemory:
    def test_write_and_read(self, project_memory):
        project_memory.write("# Title\n\nContent")
        assert project_memory.read() == "# Title\n\nContent"

    def test_empty_read_returns_empty(self, project_memory):
        assert project_memory.read() == ""

    def test_append_adds_section(self, project_memory):
        project_memory.append("Glossary", "user: a person who uses the system")
        content = project_memory.read()
        assert "## Glossary" in content
        assert "user: a person who uses the system" in content

    def test_system_prompt_block_fenced(self, project_memory):
        project_memory.write("some memory")
        block = project_memory.build_system_prompt_block()
        assert "<stage-context>" in block
        assert "some memory" in block


# ---------------------------------------------------------------------------
# PipelineDB tests
# ---------------------------------------------------------------------------


class TestPipelineDB:
    def test_create_and_get_run(self, db):
        db.create_run("r1", "Build a todo app", {"model": "claude-sonnet-4-6"})
        run = db.get_run("r1")
        assert run is not None
        assert run["idea"] == "Build a todo app"
        assert run["status"] == "running"

    def test_save_and_get_artifact(self, db):
        db.create_run("r2", "idea", {})
        db.save_artifact("r2", "discovery", {"glossary": {"x": "y"}})
        artifact = db.get_artifact("r2", "discovery")
        assert artifact == {"glossary": {"x": "y"}}

    def test_approve_artifact(self, db):
        db.create_run("r3", "idea", {})
        db.save_artifact("r3", "requirements", {"brs": "..."})
        db.approve_artifact("r3", "requirements", approved_by="cli:user")
        last = db.get_last_completed_stage("r3")
        assert last == "requirements"

    def test_audit_log(self, db):
        db.create_run("r4", "idea", {})
        db.log_decision("r4", "architecture", "APPROVED", "cli:user", "looks good")
        log = db.get_audit_log("r4")
        assert len(log) == 1
        assert log[0]["decision"] == "APPROVED"
        assert log[0]["actor"] == "cli:user"

    def test_kanban_lifecycle(self, db):
        db.create_run("r5", "idea", {})
        db.create_task("t1", "r5", "Build user API", "developer_backend")
        db.update_task_status("t1", "in_progress")
        db.update_task_status("t1", "done", metadata={"files_written": ["api.py"]})
        task = db.get_task("t1")
        assert task["status"] == "done"
        assert task["metadata"]["files_written"] == ["api.py"]

    def test_list_tasks_by_status(self, db):
        db.create_run("r6", "idea", {})
        db.create_task("t2", "r6", "UI task", "developer_ui")
        db.create_task("t3", "r6", "Backend task", "developer_backend")
        db.update_task_status("t2", "done")
        ready = db.list_tasks("r6", status="ready")
        done = db.list_tasks("r6", status="done")
        assert len(ready) == 1
        assert ready[0]["task_id"] == "t3"
        assert len(done) == 1

    def test_concurrent_writes_safe(self, db):
        db.create_run("r7", "idea", {})
        errors = []

        def _write(i):
            try:
                db.save_artifact("r7", f"stage_{i}", {"iteration": i})
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_write, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == [], f"Concurrent write errors: {errors}"


# ---------------------------------------------------------------------------
# ToolRegistry tests
# ---------------------------------------------------------------------------


class TestToolRegistry:
    def test_register_and_dispatch(self):
        reg = ToolRegistry()

        def my_tool(x: int) -> str:
            return json.dumps({"result": x * 2})

        reg.register(
            name="double",
            handler=my_tool,
            schema={"parameters": {"type": "object", "properties": {"x": {"type": "integer"}}}},
            toolset="math",
            description="Double a number",
        )
        result = reg.handle("double", {"x": 5})
        assert json.loads(result) == {"result": 10}

    def test_get_definitions_filtered_by_toolset(self):
        reg = ToolRegistry()
        reg.register("tool_a", lambda: "a", {}, toolset="set1")
        reg.register("tool_b", lambda: "b", {}, toolset="set2")
        defs = reg.get_definitions(enabled_toolsets=["set1"])
        names = [d["function"]["name"] for d in defs]
        assert "tool_a" in names
        assert "tool_b" not in names

    def test_blocked_tools_excluded(self):
        reg = ToolRegistry()
        reg.register("safe_tool", lambda: "", {}, toolset="all")
        reg.register("dangerous_tool", lambda: "", {}, toolset="all")
        defs = reg.get_definitions(blocked_tools={"dangerous_tool"})
        names = [d["function"]["name"] for d in defs]
        assert "safe_tool" in names
        assert "dangerous_tool" not in names

    def test_check_fn_gates_unavailable_tool(self):
        reg = ToolRegistry()
        reg.register("gated_tool", lambda: "", {}, toolset="all", check_fn=lambda: False)
        defs = reg.get_definitions()
        names = [d["function"]["name"] for d in defs]
        assert "gated_tool" not in names

    def test_generation_increments_on_register(self):
        reg = ToolRegistry()
        g0 = reg.generation
        reg.register("x", lambda: "", {}, toolset="s")
        assert reg.generation == g0 + 1

    def test_deregister(self):
        reg = ToolRegistry()
        reg.register("removable", lambda: "", {}, toolset="s")
        assert reg.deregister("removable") is True
        assert reg.get_entry("removable") is None

    def test_unknown_tool_raises(self):
        reg = ToolRegistry()
        with pytest.raises(KeyError):
            reg.handle("nonexistent", {})


# ---------------------------------------------------------------------------
# HITL AutoEvaluator tests
# ---------------------------------------------------------------------------


class TestAutoEvaluator:
    def test_quality_gate_passes_when_coverage_met(self):
        from gateway.hitl import AutoEvaluator, Decision

        ev = AutoEvaluator()
        decision, rationale = ev.evaluate(
            "quality_gate",
            {"coverage_pct": 85, "gate_passed": True},
            {"coverage_threshold": 80},
        )
        assert decision == Decision.APPROVED

    def test_quality_gate_fails_when_coverage_low(self):
        from gateway.hitl import AutoEvaluator, Decision

        ev = AutoEvaluator()
        decision, rationale = ev.evaluate(
            "quality_gate",
            {"coverage_pct": 70, "gate_passed": True},
            {"coverage_threshold": 80},
        )
        assert decision == Decision.REJECTED

    def test_open_questions_blocks_requirements(self):
        from gateway.hitl import AutoEvaluator, Decision

        ev = AutoEvaluator()
        decision, rationale = ev.evaluate(
            "requirements",
            {"open_questions": ["What is the retention policy?", "Who owns billing?"]},
            {"max_open_questions": 0},
        )
        assert decision == Decision.REJECTED

    def test_non_blocking_stage_approves(self):
        from gateway.hitl import AutoEvaluator, Decision

        ev = AutoEvaluator()
        decision, _ = ev.evaluate("architecture", {"adrs": []}, {})
        assert decision == Decision.APPROVED


# ---------------------------------------------------------------------------
# ApprovalQueue tests
# ---------------------------------------------------------------------------


class TestApprovalQueue:
    def test_submit_and_resolve(self):
        from gateway.hitl import ApprovalQueue, ApprovalRequest, ApprovalResponse, Decision

        q = ApprovalQueue()
        req = ApprovalRequest("r1", "run1", "architecture", {"foo": "bar"})
        q.submit(req)

        response = ApprovalResponse("r1", Decision.APPROVED, "cli:user", "looks good")

        def _resolve():
            import time

            time.sleep(0.05)
            q.resolve(response)

        threading.Thread(target=_resolve, daemon=True).start()
        resp = q.wait_for_response("r1", timeout=5.0)
        assert resp is not None
        assert resp.decision == Decision.APPROVED

    def test_timeout_returns_none(self):
        from gateway.hitl import ApprovalQueue, ApprovalRequest

        q = ApprovalQueue()
        req = ApprovalRequest("r2", "run2", "discovery", {})
        q.submit(req)
        resp = q.wait_for_response("r2", timeout=0.1)
        assert resp is None

    def test_list_pending(self):
        from gateway.hitl import ApprovalQueue, ApprovalRequest

        q = ApprovalQueue()
        q.submit(ApprovalRequest("r3", "run3", "discovery", {}))
        q.submit(ApprovalRequest("r4", "run3", "requirements", {}))
        pending = q.list_pending()
        assert len(pending) == 2


# ---------------------------------------------------------------------------
# End-to-end pipeline test (all stages, mocked model)
# ---------------------------------------------------------------------------

_ROLE_ARTIFACTS = {
    "Domain Expert": {
        "glossary": {"User": "A person using the system"},
        "bounded_contexts": [
            {"name": "Identity", "entities": ["User"], "responsibilities": "Manages user accounts"}
        ],
        "key_constraints": ["One account per email"],
        "out_of_scope": ["Social login"],
    },
    "Product Owner": {
        "user_stories": [
            {
                "id": "US-001",
                "title": "Register",
                "story": "As a visitor, I can register, so that I can use the app.",
                "priority": "must",
                "acceptance_criteria": [
                    "Given a valid email, when I register, then my account is created."
                ],
            }
        ],
        "mvp_stories": ["US-001"],
        "mvp_rationale": "Registration is the core entry point to all features.",
    },
    "Business Analyst": {
        "functional_requirements": [
            {
                "id": "FR-001",
                "story_ref": "US-001",
                "description": "The system SHALL allow user registration.",
                "priority": "must",
            }
        ],
        "non_functional_requirements": [
            {
                "id": "NFR-001",
                "category": "security",
                "description": "Passwords stored as bcrypt hashes.",
            }
        ],
        "data_dictionary": [
            {
                "entity": "User",
                "attributes": ["email: string, unique"],
                "constraints": ["Email must be verified"],
            }
        ],
        "process_flows": [
            {"name": "Registration", "steps": ["Submit form", "Validate", "Create account"]}
        ],
        "open_questions": [],
        "assumptions": ["Email is the primary identifier"],
    },
    "Architect": {
        "adrs": [
            {
                "id": "ADR-001",
                "title": "Use Python + FastAPI",
                "status": "accepted",
                "context": "Need a lightweight async API framework.",
                "options_considered": ["FastAPI", "Flask", "Django"],
                "decision": "FastAPI — async, auto-generated OpenAPI docs.",
                "consequences": {
                    "positive": ["Auto-generated docs"],
                    "negative": ["Smaller ecosystem than Django"],
                },
            }
        ],
        "c4_context": "graph LR\n  User --> App",
        "c4_container": "graph LR\n  App --> DB",
        "tech_stack": {
            "language": "Python 3.11",
            "framework": "FastAPI",
            "database": "SQLite",
            "cache": "None",
            "frontend": "React 18",
            "infra": "Docker",
        },
        "api_style": "REST",
        "auth_strategy": "JWT Bearer tokens, 15-min expiry.",
    },
    "Tech Lead": {
        "tasks": [
            {
                "task_id": "t-001",
                "title": "Implement user model and registration endpoint",
                "assignee_role": "developer_backend",
                "description": "Create User SQLAlchemy model, Alembic migration, and POST /register endpoint.",
                "acceptance_criteria": [
                    "Given valid email and password, POST /register returns 201.",
                    "Duplicate email returns 409.",
                ],
                "dependencies": [],
                "skills": ["python", "fastapi", "sqlalchemy"],
            },
            {
                "task_id": "t-002",
                "title": "Build registration form UI",
                "assignee_role": "developer_ui",
                "description": "Create React registration form with client-side validation.",
                "acceptance_criteria": [
                    "Form submits to POST /register.",
                    "All fields have ARIA labels.",
                ],
                "dependencies": ["t-001"],
                "skills": ["react", "typescript"],
            },
            {
                "task_id": "t-003",
                "title": "Write Dockerfile and CI pipeline",
                "assignee_role": "developer_devops",
                "description": "Multi-stage Dockerfile and GitHub Actions CI workflow.",
                "acceptance_criteria": [
                    "docker build succeeds.",
                    "CI runs lint + test + build.",
                ],
                "dependencies": [],
                "skills": ["docker", "github-actions"],
            },
        ],
        "coding_standards": {
            "language_version": "Python 3.11",
            "style_guide": "ruff + black",
            "test_framework": "pytest",
            "coverage_minimum": 80,
            "pr_checklist": ["All tests pass", "No hardcoded secrets"],
        },
        "sprint_plan": [{"sprint": 1, "tasks": ["t-001", "t-002", "t-003"]}],
    },
    "Backend Developer": {
        "files_written": ["src/api/users.py", "tests/test_users.py"],
        "tests_passing": True,
        "coverage_pct": 85,
        "pr_description": "Implemented user registration endpoint with JWT auth.",
    },
    "UI Developer": {
        "files_written": ["src/components/RegisterForm.tsx", "tests/RegisterForm.test.tsx"],
        "tests_passing": True,
        "coverage_pct": 82,
        "pr_description": "Registration form with full ARIA accessibility.",
        "accessibility_notes": "All inputs labelled; tab order verified.",
    },
    "DevOps Engineer": {
        "files_written": ["Dockerfile", "docker-compose.yml", ".github/workflows/ci.yml"],
        "ci_steps": ["lint", "test", "build"],
        "secrets_required": ["DATABASE_URL"],
        "deployment_notes": "Deploy via docker-compose on any Linux host.",
    },
    "QA Engineer": {
        "test_plan": "Run pytest --cov with coverage threshold 80%.",
        "test_suites_run": ["tests/test_users.py"],
        "total_tests": 10,
        "passing": 10,
        "failing": 0,
        "coverage_pct": 85,
        "bugs": [],
        "acceptance_criteria_coverage": {"US-001": "covered"},
        "gate_passed": True,
        "gate_rationale": "All 10 tests pass; coverage 85% >= 80% threshold.",
    },
    "Release Manager": {
        "version": "0.1.0",
        "release_notes": "## What's new\n- User registration feature",
        "deployment_checklist": ["Pull image", "Run migrations", "Deploy", "Smoke test"],
        "rollback_procedure": "Re-deploy the previous image tag and roll back migration.",
        "release_tag": "v0.1.0",
        "deployment_manifest": "Version: 0.1.0\nDocker image: app:0.1.0",
    },
}


def _mock_call_model(model, messages, *, system="", tools=None, **kwargs):
    """Return a canned artifact response based on which role is calling."""
    artifact = None
    for role_display, role_artifact in _ROLE_ARTIFACTS.items():
        if f"Role: {role_display}" in system:
            artifact = role_artifact
            break

    if artifact is None:
        artifact = {"fallback": True}

    content = f"Analysis complete.\n\n```json\n{json.dumps(artifact, indent=2)}\n```"
    return {
        "message": {"role": "assistant", "content": content},
        "stop_reason": "end_turn",
        "usage": {"prompt_tokens": 50, "completion_tokens": 100},
    }


class TestPipelineE2E:
    """End-to-end pipeline test: all 7 stages, mocked model, auto-approve gates."""

    @patch("core.agent_runtime.call_model_openai", side_effect=_mock_call_model)
    def test_full_pipeline_completes(self, mock_model, tmp_path):
        from core.pipeline import SDLCPipeline
        from core.session import PipelineDB

        db = PipelineDB(db_path=tmp_path / "e2e.db")
        config = {
            "workspace": str(tmp_path / "output"),
            "governance": {"default_mode": "auto"},
            "pipeline": {"model": "claude-sonnet-4-6"},
        }
        pipeline = SDLCPipeline.from_dict(config, db=db)
        result = pipeline.run("Build a simple user registration app")

        assert result["status"] == "completed", f"Pipeline failed: {result}"
        assert result["run_id"].startswith("run-")

    @patch("core.agent_runtime.call_model_openai", side_effect=_mock_call_model)
    def test_all_stage_artifacts_persisted(self, mock_model, tmp_path):
        from core.pipeline import SDLCPipeline, SDLC_STAGES
        from core.session import PipelineDB

        db = PipelineDB(db_path=tmp_path / "e2e2.db")
        config = {
            "workspace": str(tmp_path / "output"),
            "governance": {"default_mode": "auto"},
        }
        pipeline = SDLCPipeline.from_dict(config, db=db)
        result = pipeline.run("Build a task tracker")
        run_id = result["run_id"]

        for stage in SDLC_STAGES:
            artifact = db.get_artifact(run_id, stage.name)
            assert artifact is not None, f"No artifact for stage: {stage.name}"
            assert isinstance(artifact, dict)

    @patch("core.agent_runtime.call_model_openai", side_effect=_mock_call_model)
    def test_workspace_directory_created(self, mock_model, tmp_path):
        from core.pipeline import SDLCPipeline
        from core.session import PipelineDB

        workspace = tmp_path / "output"
        db = PipelineDB(db_path=tmp_path / "e2e3.db")
        config = {
            "workspace": str(workspace),
            "governance": {"default_mode": "auto"},
        }
        pipeline = SDLCPipeline.from_dict(config, db=db)
        result = pipeline.run("Build a chat app")
        run_id = result["run_id"]

        assert (workspace / run_id).exists(), f"Workspace dir missing for {run_id}"

    @patch("core.agent_runtime.call_model_openai", side_effect=_mock_call_model)
    def test_audit_log_populated(self, mock_model, tmp_path):
        from core.pipeline import SDLCPipeline
        from core.session import PipelineDB

        db = PipelineDB(db_path=tmp_path / "e2e4.db")
        config = {
            "workspace": str(tmp_path / "output"),
            "governance": {"default_mode": "auto"},
        }
        pipeline = SDLCPipeline.from_dict(config, db=db)
        result = pipeline.run("Build a notes app")
        run_id = result["run_id"]

        audit = db.get_audit_log(run_id)
        # There are 4 HITL gates (requirements, architecture, quality_gate, delivery)
        assert len(audit) >= 4, f"Expected ≥4 audit entries, got {len(audit)}"
        decisions = {entry["decision"] for entry in audit}
        assert "APPROVED" in decisions

    @patch("core.agent_runtime.call_model_openai", side_effect=_mock_call_model)
    def test_resume_skips_completed_stages(self, mock_model, tmp_path):
        from core.pipeline import SDLCPipeline
        from core.session import PipelineDB

        db = PipelineDB(db_path=tmp_path / "e2e5.db")
        config = {
            "workspace": str(tmp_path / "output"),
            "governance": {"default_mode": "auto"},
        }
        pipeline = SDLCPipeline.from_dict(config, db=db)

        # Run the full pipeline first
        result = pipeline.run("Build an invoice app")
        assert result["status"] == "completed"
        run_id = result["run_id"]
        first_call_count = mock_model.call_count

        # Resume should skip all already-approved stages → no extra model calls
        result2 = pipeline.run("", resume_run_id=run_id)
        assert result2["status"] == "completed"
        assert (
            mock_model.call_count == first_call_count
        ), "Resume should not call the model again for already-approved stages"
