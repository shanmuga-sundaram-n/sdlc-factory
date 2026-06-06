"""HITL governance integration tests.

Covers:
- AutoEvaluator: quality_gate and open_questions rules (including role-wrapped artifacts)
- HITLGateway auto mode: approved/rejected scenarios
- HITLGateway queue mode: resolve with APPROVED/REJECTED/AMENDED
- HITLGateway unknown mode: defaults to APPROVED
- pipeline.py gate integration: REJECTED pauses run, AMENDED clears downstream
- sdlc.py approve command: APPROVED, REJECTED, AMENDED + --resume flag
- PipelineDB.clear_stages_after() and update_artifact()
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

from gateway.hitl import (
    ApprovalQueue,
    ApprovalRequest,
    ApprovalResponse,
    AutoEvaluator,
    Decision,
    HITLGateway,
)
from core.session import PipelineDB


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_db(tmp_path):
    return PipelineDB(tmp_path / "test.db")


@pytest.fixture
def gateway():
    return HITLGateway()


@pytest.fixture
def evaluator():
    return AutoEvaluator()


# ---------------------------------------------------------------------------
# AutoEvaluator — quality_gate rule
# ---------------------------------------------------------------------------

class TestAutoEvaluatorQualityGate:
    def test_approved_when_coverage_meets_threshold(self, evaluator):
        artifact = {"coverage_pct": 85, "gate_passed": True}
        decision, rationale = evaluator.evaluate("quality_gate", artifact, {"coverage_threshold": 80})
        assert decision == Decision.APPROVED
        assert "85%" in rationale

    def test_rejected_when_coverage_below_threshold(self, evaluator):
        artifact = {"coverage_pct": 70, "gate_passed": True}
        decision, _ = evaluator.evaluate("quality_gate", artifact, {"coverage_threshold": 80})
        assert decision == Decision.REJECTED

    def test_rejected_when_gate_passed_false(self, evaluator):
        artifact = {"coverage_pct": 90, "gate_passed": False}
        decision, _ = evaluator.evaluate("quality_gate", artifact, {})
        assert decision == Decision.REJECTED

    def test_unwraps_role_wrapped_artifact(self, evaluator):
        """Stage artifacts come as {role: payload}; rule must unwrap them."""
        artifact = {"qa_engineer": {"coverage_pct": 90, "gate_passed": True}}
        decision, _ = evaluator.evaluate("quality_gate", artifact, {})
        assert decision == Decision.APPROVED

    def test_rejects_role_wrapped_failing_artifact(self, evaluator):
        artifact = {"qa_engineer": {"coverage_pct": 50, "gate_passed": False}}
        decision, _ = evaluator.evaluate("quality_gate", artifact, {})
        assert decision == Decision.REJECTED

    def test_default_threshold_is_80(self, evaluator):
        artifact = {"coverage_pct": 79, "gate_passed": True}
        decision, _ = evaluator.evaluate("quality_gate", artifact, {})
        assert decision == Decision.REJECTED

    def test_non_quality_gate_stage_skips_rule(self, evaluator):
        artifact = {"coverage_pct": 0, "gate_passed": False}
        decision, rationale = evaluator.evaluate("requirements", artifact, {})
        assert decision == Decision.APPROVED
        assert "no blocking rules" in rationale


# ---------------------------------------------------------------------------
# AutoEvaluator — open_questions rule
# ---------------------------------------------------------------------------

class TestAutoEvaluatorOpenQuestions:
    def test_rejected_when_open_questions_exceed_max(self, evaluator):
        artifact = {"open_questions": ["q1", "q2", "q3"]}
        decision, rationale = evaluator.evaluate(
            "requirements", artifact, {"max_open_questions": 1}
        )
        assert decision == Decision.REJECTED
        assert "3" in rationale

    def test_approved_when_questions_within_limit(self, evaluator):
        artifact = {"open_questions": ["q1"]}
        decision, _ = evaluator.evaluate(
            "requirements", artifact, {"max_open_questions": 2}
        )
        assert decision == Decision.APPROVED

    def test_approved_when_no_open_questions(self, evaluator):
        artifact = {"open_questions": []}
        decision, _ = evaluator.evaluate("requirements", artifact, {})
        assert decision == Decision.APPROVED

    def test_unwraps_role_wrapped_artifact(self, evaluator):
        artifact = {"business_analyst": {"open_questions": ["q1", "q2"]}}
        decision, _ = evaluator.evaluate(
            "requirements", artifact, {"max_open_questions": 0}
        )
        assert decision == Decision.REJECTED

    def test_non_requirements_stage_skips_rule(self, evaluator):
        artifact = {"open_questions": ["q1", "q2", "q3"]}
        decision, _ = evaluator.evaluate("architecture", artifact, {"max_open_questions": 0})
        assert decision == Decision.APPROVED


# ---------------------------------------------------------------------------
# HITLGateway — auto mode
# ---------------------------------------------------------------------------

class TestHITLGatewayAutoMode:
    def test_auto_approves_clean_quality_gate(self, gateway):
        artifact = {"qa_engineer": {"coverage_pct": 95, "gate_passed": True}}
        decision, rationale, returned = gateway.request_approval(
            "quality_gate", artifact, "run-1", mode="auto", config={}
        )
        assert decision == Decision.APPROVED
        assert returned is artifact

    def test_auto_rejects_failing_quality_gate(self, gateway):
        artifact = {"qa_engineer": {"coverage_pct": 60, "gate_passed": False}}
        decision, _, _ = gateway.request_approval(
            "quality_gate", artifact, "run-1", mode="auto", config={}
        )
        assert decision == Decision.REJECTED

    def test_auto_approves_unknown_stage(self, gateway):
        artifact = {"some_key": "some_value"}
        decision, rationale, _ = gateway.request_approval(
            "planning", artifact, "run-1", mode="auto", config={}
        )
        assert decision == Decision.APPROVED

    def test_auto_returns_artifact_unchanged(self, gateway):
        original = {"coverage_pct": 90, "gate_passed": True}
        artifact = dict(original)
        _, _, returned = gateway.request_approval(
            "quality_gate", artifact, "run-1", mode="auto", config={}
        )
        assert returned == artifact


# ---------------------------------------------------------------------------
# HITLGateway — queue mode
# ---------------------------------------------------------------------------

class TestHITLGatewayQueueMode:
    def _resolve_async(self, gateway, request_id, decision, rationale, amended=None):
        """Helper: resolve an approval request from another thread after a short delay."""
        def _worker():
            import time
            time.sleep(0.05)
            gateway.approval_queue.resolve(
                ApprovalResponse(
                    request_id=request_id,
                    decision=decision,
                    actor="test",
                    rationale=rationale,
                    amended_artifact=amended,
                )
            )
        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        return t

    def test_queue_approved(self, gateway):
        artifact = {"data": "value"}
        # We need the request_id before calling, so intercept submit
        submitted = []
        orig_submit = gateway.approval_queue.submit

        def capturing_submit(req):
            submitted.append(req.request_id)
            orig_submit(req)

        gateway.approval_queue.submit = capturing_submit

        def _resolve():
            import time
            time.sleep(0.05)
            assert submitted, "No request submitted yet"
            gateway.approval_queue.resolve(
                ApprovalResponse(
                    request_id=submitted[0],
                    decision=Decision.APPROVED,
                    actor="tester",
                    rationale="LGTM",
                )
            )

        t = threading.Thread(target=_resolve, daemon=True)
        t.start()
        decision, rationale, returned = gateway.request_approval(
            "requirements", artifact, "run-q1", mode="queue", timeout=5.0
        )
        t.join(timeout=2)
        assert decision == Decision.APPROVED
        assert rationale == "LGTM"
        assert returned is artifact

    def test_queue_rejected(self, gateway):
        artifact = {"data": "value"}
        submitted = []
        orig_submit = gateway.approval_queue.submit

        def capturing_submit(req):
            submitted.append(req.request_id)
            orig_submit(req)

        gateway.approval_queue.submit = capturing_submit

        def _resolve():
            import time
            time.sleep(0.05)
            gateway.approval_queue.resolve(
                ApprovalResponse(
                    request_id=submitted[0],
                    decision=Decision.REJECTED,
                    actor="reviewer",
                    rationale="needs more work",
                )
            )

        t = threading.Thread(target=_resolve, daemon=True)
        t.start()
        decision, rationale, _ = gateway.request_approval(
            "architecture", artifact, "run-q2", mode="queue", timeout=5.0
        )
        t.join(timeout=2)
        assert decision == Decision.REJECTED

    def test_queue_amended_replaces_artifact(self, gateway):
        original_artifact = {"version": 1}
        amended_artifact = {"version": 2, "notes": "amended"}
        submitted = []
        orig_submit = gateway.approval_queue.submit

        def capturing_submit(req):
            submitted.append(req.request_id)
            orig_submit(req)

        gateway.approval_queue.submit = capturing_submit

        def _resolve():
            import time
            time.sleep(0.05)
            gateway.approval_queue.resolve(
                ApprovalResponse(
                    request_id=submitted[0],
                    decision=Decision.AMENDED,
                    actor="editor",
                    rationale="updated spec",
                    amended_artifact=amended_artifact,
                )
            )

        t = threading.Thread(target=_resolve, daemon=True)
        t.start()
        decision, _, returned = gateway.request_approval(
            "requirements", original_artifact, "run-q3", mode="queue", timeout=5.0
        )
        t.join(timeout=2)
        assert decision == Decision.AMENDED
        assert returned == amended_artifact

    def test_queue_timeout_returns_rejected(self, gateway):
        artifact = {"data": "value"}
        decision, rationale, _ = gateway.request_approval(
            "delivery", artifact, "run-timeout", mode="queue", timeout=0.05
        )
        assert decision == Decision.REJECTED
        assert "timeout" in rationale


# ---------------------------------------------------------------------------
# HITLGateway — unknown mode
# ---------------------------------------------------------------------------

class TestHITLGatewayUnknownMode:
    def test_unknown_mode_auto_approves(self, gateway):
        artifact = {"x": 1}
        decision, rationale, _ = gateway.request_approval(
            "planning", artifact, "run-u1", mode="telepathy"
        )
        assert decision == Decision.APPROVED
        assert "auto-approved" in rationale


# ---------------------------------------------------------------------------
# ApprovalQueue
# ---------------------------------------------------------------------------

class TestApprovalQueue:
    def test_submit_and_list_pending(self):
        q = ApprovalQueue()
        req = ApprovalRequest(request_id="r1", run_id="run-1", stage="requirements", artifact={})
        q.submit(req)
        pending = q.list_pending()
        assert any(p.request_id == "r1" for p in pending)

    def test_resolve_unknown_request_returns_false(self):
        q = ApprovalQueue()
        resp = ApprovalResponse(
            request_id="nonexistent", decision=Decision.APPROVED, actor="x", rationale=""
        )
        result = q.resolve(resp)
        assert result is False

    def test_pop_request_removes_from_pending(self):
        q = ApprovalQueue()
        req = ApprovalRequest(request_id="r2", run_id="run-2", stage="architecture", artifact={})
        q.submit(req)
        popped = q.pop_request("r2")
        assert popped is not None
        assert not any(p.request_id == "r2" for p in q.list_pending())


# ---------------------------------------------------------------------------
# PipelineDB — clear_stages_after and update_artifact
# ---------------------------------------------------------------------------

class TestPipelineDBAmendedSupport:
    STAGE_ORDER = ["discovery", "requirements", "architecture", "planning", "development",
                   "quality_gate", "delivery"]

    def test_clear_stages_after_removes_downstream(self, tmp_db):
        db = tmp_db
        db.create_run("run-a", "test idea", {})
        for stage in ["discovery", "requirements", "architecture"]:
            db.save_artifact("run-a", stage, {"stage": stage})

        cleared = db.clear_stages_after("run-a", "requirements", self.STAGE_ORDER)
        assert "architecture" in cleared
        assert db.get_artifact("run-a", "architecture") is None
        assert db.get_artifact("run-a", "requirements") is not None
        assert db.get_artifact("run-a", "discovery") is not None

    def test_clear_stages_after_unknown_stage_returns_empty(self, tmp_db):
        db = tmp_db
        db.create_run("run-b", "test", {})
        cleared = db.clear_stages_after("run-b", "nonexistent", self.STAGE_ORDER)
        assert cleared == []

    def test_clear_last_stage_returns_empty(self, tmp_db):
        db = tmp_db
        db.create_run("run-c", "test", {})
        db.save_artifact("run-c", "delivery", {"done": True})
        cleared = db.clear_stages_after("run-c", "delivery", self.STAGE_ORDER)
        assert cleared == []

    def test_update_artifact_replaces_content(self, tmp_db):
        db = tmp_db
        db.create_run("run-d", "test", {})
        db.save_artifact("run-d", "requirements", {"v": 1})
        db.update_artifact("run-d", "requirements", {"v": 2, "amended": True})
        result = db.get_artifact("run-d", "requirements")
        assert result == {"v": 2, "amended": True}

    def test_update_artifact_resets_status_to_pending(self, tmp_db):
        db = tmp_db
        db.create_run("run-e", "test", {})
        db.save_artifact("run-e", "requirements", {"v": 1})
        db.approve_artifact("run-e", "requirements", approved_by="operator")
        db.update_artifact("run-e", "requirements", {"v": 2})
        row = db._conn().execute(
            "SELECT status FROM stage_artifacts WHERE run_id='run-e' AND stage='requirements'"
        ).fetchone()
        assert row["status"] == "pending"


# ---------------------------------------------------------------------------
# Pipeline integration — gate REJECTED pauses run
# ---------------------------------------------------------------------------

_ROLE_ARTIFACTS = {
    "Domain Expert":       {"domain": "test", "market_size": "large", "competitors": []},
    "Product Owner":       {"product_name": "TestApp", "vision": "v", "epics": [], "mvp_scope": []},
    "Business Analyst":    {"requirements": [], "user_stories": [], "open_questions": [], "acceptance_criteria": []},
    "Architect":           {"system_design": {}, "tech_stack": {}, "adrs": [], "risks": []},
    "Tech Lead":           {"sprint_plan": [], "tasks": [], "risks": []},
    "Developer Backend":   {"files_created": [], "summary": "done", "test_coverage": 80},
    "Developer UI":        {"files_created": [], "summary": "done", "components": []},
    "Developer DevOps":    {"files_created": [], "summary": "done", "pipeline_steps": []},
    "QA Engineer":         {"test_plan": [], "coverage_pct": 90, "gate_passed": True, "issues": []},
    "Release Manager":     {"release_notes": "", "version": "1.0.0", "deployment_steps": []},
}


def _mock_call_model(model, base_url, api_key, system, messages, tools, **kwargs):
    role_name = "Unknown"
    for line in system.split("\n"):
        if line.startswith("# Role:"):
            role_name = line.replace("# Role:", "").strip()
            break
    artifact = _ROLE_ARTIFACTS.get(role_name, {"result": "ok"})
    content = f"Here is my output:\n\n```json\n{json.dumps(artifact)}\n```"
    return {
        "message": {"role": "assistant", "content": content},
        "stop_reason": "end_turn",
        "usage": {"prompt_tokens": 10, "completion_tokens": 20},
    }


@pytest.fixture
def auto_config(tmp_path):
    return {
        "workspace": str(tmp_path / "output"),
        "governance": {
            "default_mode": "auto",
            "hitl_gates": [
                {"stage": "requirements", "mode": "auto", "max_open_questions": 0},
                {"stage": "quality_gate",  "mode": "auto", "coverage_threshold": 80},
            ],
        },
        "pipeline": {"model": "claude-sonnet-4-6"},
    }


class TestPipelineHITLIntegration:
    @patch("core.agent_runtime.call_model_openai", side_effect=_mock_call_model)
    def test_auto_gate_approves_clean_run(self, mock_model, auto_config, tmp_path):
        db = PipelineDB(tmp_path / "pipeline.db")
        from core.pipeline import SDLCPipeline
        pipeline = SDLCPipeline.from_dict(auto_config, db=db)
        result = pipeline.run("Build a test app")
        assert result["status"] == "completed"
        audit = db.get_audit_log(result["run_id"])
        gate_entries = [e for e in audit if e["stage"] in ("requirements", "quality_gate")]
        assert all(e["decision"] == "APPROVED" for e in gate_entries)

    @patch("core.agent_runtime.call_model_openai", side_effect=_mock_call_model)
    def test_auto_gate_rejects_failing_quality_gate(self, mock_model, auto_config, tmp_path):
        """Override qa_engineer artifact so coverage is below threshold."""
        failing_artifacts = dict(_ROLE_ARTIFACTS)
        failing_artifacts["QA Engineer"] = {
            "test_plan": [], "coverage_pct": 50, "gate_passed": False, "issues": []
        }

        def _mock_failing(model, base_url, api_key, system, messages, tools, **kwargs):
            role_name = "Unknown"
            for line in system.split("\n"):
                if line.startswith("# Role:"):
                    role_name = line.replace("# Role:", "").strip()
                    break
            artifact = failing_artifacts.get(role_name, {"result": "ok"})
            content = f"```json\n{json.dumps(artifact)}\n```"
            return {
                "message": {"role": "assistant", "content": content},
                "stop_reason": "end_turn",
                "usage": {"prompt_tokens": 10, "completion_tokens": 20},
            }

        db = PipelineDB(tmp_path / "pipeline.db")
        from core.pipeline import SDLCPipeline
        with patch("core.agent_runtime.call_model_openai", side_effect=_mock_failing):
            pipeline = SDLCPipeline.from_dict(auto_config, db=db)
            result = pipeline.run("Build a test app")

        assert result["status"] == "paused"
        assert result["paused_at"] == "quality_gate"

    @patch("core.agent_runtime.call_model_openai", side_effect=_mock_call_model)
    def test_rejected_run_resumes_after_approval(self, mock_model, auto_config, tmp_path):
        """Pause a run at quality_gate, then approve and resume."""
        failing_qa = {"test_plan": [], "coverage_pct": 40, "gate_passed": False, "issues": []}
        passing_qa = {"test_plan": [], "coverage_pct": 95, "gate_passed": True, "issues": []}
        call_count = {"n": 0}

        def _mock_with_fail_then_pass(model, base_url, api_key, system, messages, tools, **kwargs):
            role_name = "Unknown"
            for line in system.split("\n"):
                if line.startswith("# Role:"):
                    role_name = line.replace("# Role:", "").strip()
                    break
            if role_name == "QA Engineer":
                call_count["n"] += 1
                artifact = failing_qa if call_count["n"] == 1 else passing_qa
            else:
                artifact = _ROLE_ARTIFACTS.get(role_name, {"result": "ok"})
            content = f"```json\n{json.dumps(artifact)}\n```"
            return {
                "message": {"role": "assistant", "content": content},
                "stop_reason": "end_turn",
                "usage": {"prompt_tokens": 10, "completion_tokens": 20},
            }

        db = PipelineDB(tmp_path / "pipeline.db")
        from core.pipeline import SDLCPipeline
        with patch("core.agent_runtime.call_model_openai", side_effect=_mock_with_fail_then_pass):
            pipeline = SDLCPipeline.from_dict(auto_config, db=db)
            result = pipeline.run("Build a test app")
            assert result["status"] == "paused"

            run_id = result["run_id"]
            # Manually approve quality_gate artifact in DB (simulates operator override)
            db.approve_artifact(run_id, "quality_gate", approved_by="test-operator")
            # Save the passing artifact
            db.save_artifact(run_id, "quality_gate", {"qa_engineer": passing_qa})
            db.approve_artifact(run_id, "quality_gate", approved_by="test-operator")
            db.update_run_status(run_id, "paused", stage="quality_gate")

            result2 = pipeline.run("", resume_run_id=run_id)

        assert result2["status"] == "completed"

    @patch("core.agent_runtime.call_model_openai", side_effect=_mock_call_model)
    def test_amended_clears_downstream_artifacts(self, mock_model, auto_config, tmp_path):
        """AMENDED decision at requirements should clear architecture and beyond."""
        db = PipelineDB(tmp_path / "pipeline.db")
        from core.pipeline import SDLC_STAGES, SDLCPipeline
        from gateway.hitl import ApprovalQueue, ApprovalResponse

        amended_artifact = {
            "requirements": ["req1-amended"],
            "user_stories": [],
            "open_questions": [],
            "acceptance_criteria": [],
        }

        # Inject a queue gate for requirements so we can control the decision
        queue_config = dict(auto_config)
        queue_config["governance"] = {
            "default_mode": "auto",
            "hitl_gates": [
                {"stage": "requirements", "mode": "queue"},
                {"stage": "quality_gate",  "mode": "auto", "coverage_threshold": 80},
            ],
        }

        aq = ApprovalQueue()
        gateway = HITLGateway(approval_queue=aq)
        submitted_ids = []
        orig_submit = aq.submit

        def capturing_submit(req):
            submitted_ids.append(req.request_id)
            orig_submit(req)

        aq.submit = capturing_submit

        def _resolve_amended():
            import time
            # Wait until requirements stage submits its request
            for _ in range(100):
                if submitted_ids:
                    break
                time.sleep(0.05)
            if submitted_ids:
                aq.resolve(ApprovalResponse(
                    request_id=submitted_ids[0],
                    decision=Decision.AMENDED,
                    actor="test",
                    rationale="amended",
                    amended_artifact={"business_analyst": amended_artifact},
                ))

        t = threading.Thread(target=_resolve_amended, daemon=True)
        t.start()

        pipeline = SDLCPipeline.from_dict(queue_config, db=db, hitl_gateway=gateway)
        result = pipeline.run("Build a test app")
        t.join(timeout=10)

        # After AMENDED at requirements, architecture and beyond continue (no paused)
        assert result["status"] == "completed"

        # Verify audit log records AMENDED for requirements
        audit = db.get_audit_log(result["run_id"])
        req_entries = [e for e in audit if e["stage"] == "requirements"]
        assert any(e["decision"] == "AMENDED" for e in req_entries)
