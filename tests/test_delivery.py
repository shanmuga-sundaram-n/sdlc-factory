"""Phase 5 delivery tests.

Covers:
- tools/docker_tool: docker_build, docker_run, docker_image_exists (mocked subprocess)
- PipelineDB token_usage: record_token_usage, get_token_summary, schema migration
- sdlc.py cmd_deliver: completed/incomplete run checks, docker build, no-docker flag
- sdlc.py cmd_cost: empty data, single row, multi-row with totals
- sdlc.py --dry-run: plan output, no API calls made
"""

from __future__ import annotations

import json
import sys
from argparse import Namespace
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.session import PipelineDB


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_db(tmp_path):
    db = PipelineDB(tmp_path / "test.db")
    db.create_run("run-1", "test idea", {"workspace": str(tmp_path / "output")})
    return db


# ---------------------------------------------------------------------------
# Docker tools — docker_build
# ---------------------------------------------------------------------------

class TestDockerBuildTool:
    def _setup_workspace(self, tmp_path: Path) -> None:
        from tools._context import set_tool_context
        set_tool_context(workspace=tmp_path)
        (tmp_path / "Dockerfile").write_text("FROM python:3.12-slim\n", encoding="utf-8")

    def test_build_success_returns_success_true(self, tmp_path):
        from tools.docker_tool import docker_build
        self._setup_workspace(tmp_path)

        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = "Successfully built abc123456789\nStep 1/1 : FROM python:3.12-slim\n"
        proc.stderr = ""

        with patch("subprocess.run", return_value=proc):
            result = json.loads(docker_build(tag="testapp:1.0"))

        assert result["success"] is True
        assert result["tag"] == "testapp:1.0"

    def test_build_extracts_image_id(self, tmp_path):
        from tools.docker_tool import docker_build
        self._setup_workspace(tmp_path)

        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = "Successfully built abc123456789\n"
        proc.stderr = ""

        with patch("subprocess.run", return_value=proc):
            result = json.loads(docker_build(tag="testapp:1.0"))

        assert result["image_id"] == "abc123456789"

    def test_build_failure_returns_success_false(self, tmp_path):
        from tools.docker_tool import docker_build
        self._setup_workspace(tmp_path)

        proc = MagicMock()
        proc.returncode = 1
        proc.stdout = ""
        proc.stderr = "ERROR: no such file or directory"

        with patch("subprocess.run", return_value=proc):
            result = json.loads(docker_build(tag="testapp:1.0"))

        assert result["success"] is False
        assert result["return_code"] == 1

    def test_build_timeout_returns_error(self, tmp_path):
        from tools.docker_tool import docker_build
        from tools._context import set_tool_context
        import subprocess
        set_tool_context(workspace=tmp_path)

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="docker", timeout=1)):
            result = json.loads(docker_build(tag="myapp:latest", timeout=1))

        assert result["success"] is False
        assert "timed out" in result["stderr"]

    def test_docker_not_found_returns_error(self, tmp_path):
        from tools.docker_tool import docker_build
        from tools._context import set_tool_context
        set_tool_context(workspace=tmp_path)

        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = json.loads(docker_build(tag="myapp:latest"))

        assert result["success"] is False
        assert "not found" in result["stderr"]

    def test_build_with_build_args(self, tmp_path):
        from tools.docker_tool import docker_build
        self._setup_workspace(tmp_path)

        proc = MagicMock(returncode=0, stdout="", stderr="")
        captured_cmd = []

        def fake_run(cmd, **kwargs):
            captured_cmd.extend(cmd)
            return proc

        with patch("subprocess.run", side_effect=fake_run):
            docker_build(tag="myapp:1.0", build_args={"APP_VERSION": "1.0", "ENV": "prod"})

        assert "--build-arg" in captured_cmd
        assert "APP_VERSION=1.0" in captured_cmd


# ---------------------------------------------------------------------------
# Docker tools — docker_run
# ---------------------------------------------------------------------------

class TestDockerRunTool:
    def test_run_success(self, tmp_path):
        from tools.docker_tool import docker_run
        from tools._context import set_tool_context
        set_tool_context(workspace=tmp_path)

        proc = MagicMock(returncode=0, stdout="Hello World\n", stderr="")
        with patch("subprocess.run", return_value=proc):
            result = json.loads(docker_run(tag="myapp:latest", command="--help"))

        assert result["success"] is True
        assert result["exit_code"] == 0

    def test_run_passes_env_vars(self, tmp_path):
        from tools.docker_tool import docker_run
        from tools._context import set_tool_context
        set_tool_context(workspace=tmp_path)

        proc = MagicMock(returncode=0, stdout="", stderr="")
        captured = []

        def fake_run(cmd, **kwargs):
            captured.extend(cmd)
            return proc

        with patch("subprocess.run", side_effect=fake_run):
            docker_run(tag="myapp:latest", env={"DB_URL": "sqlite://"})

        assert "-e" in captured
        assert "DB_URL=sqlite://" in captured

    def test_run_adds_rm_flag(self, tmp_path):
        from tools.docker_tool import docker_run
        from tools._context import set_tool_context
        set_tool_context(workspace=tmp_path)

        proc = MagicMock(returncode=0, stdout="", stderr="")
        captured = []

        def fake_run(cmd, **kwargs):
            captured.extend(cmd)
            return proc

        with patch("subprocess.run", side_effect=fake_run):
            docker_run(tag="myapp:latest")

        assert "--rm" in captured


# ---------------------------------------------------------------------------
# Docker tools — docker_image_exists
# ---------------------------------------------------------------------------

class TestDockerImageExists:
    def test_exists_when_inspect_returns_zero(self, tmp_path):
        from tools.docker_tool import docker_image_exists
        from tools._context import set_tool_context
        set_tool_context(workspace=tmp_path)

        proc = MagicMock(returncode=0, stdout="[{...}]", stderr="")
        with patch("subprocess.run", return_value=proc):
            result = json.loads(docker_image_exists("myapp:latest"))

        assert result["exists"] is True

    def test_not_exists_when_inspect_returns_nonzero(self, tmp_path):
        from tools.docker_tool import docker_image_exists
        from tools._context import set_tool_context
        set_tool_context(workspace=tmp_path)

        proc = MagicMock(returncode=1, stdout="", stderr="Error: No such image")
        with patch("subprocess.run", return_value=proc):
            result = json.loads(docker_image_exists("nonexistent:latest"))

        assert result["exists"] is False


# ---------------------------------------------------------------------------
# PipelineDB — token_usage
# ---------------------------------------------------------------------------

class TestTokenUsageDB:
    def test_record_and_retrieve(self, tmp_db):
        tmp_db.record_token_usage(
            "run-1", "discovery", "domain_expert",
            tokens_in=1000, tokens_out=500, duration_seconds=3.2,
            model="claude-sonnet-4-6",
        )
        rows = tmp_db.get_token_summary("run-1")
        assert len(rows) == 1
        assert rows[0]["tokens_in"] == 1000
        assert rows[0]["tokens_out"] == 500
        assert rows[0]["role"] == "domain_expert"
        assert rows[0]["model"] == "claude-sonnet-4-6"

    def test_multiple_records_aggregated_per_role(self, tmp_db):
        for _ in range(3):
            tmp_db.record_token_usage(
                "run-1", "development", "developer_backend",
                tokens_in=200, tokens_out=100, model="claude-sonnet-4-6",
            )
        rows = tmp_db.get_token_summary("run-1")
        dev_row = next(r for r in rows if r["role"] == "developer_backend")
        assert dev_row["tokens_in"] == 600
        assert dev_row["tokens_out"] == 300

    def test_returns_empty_for_unknown_run(self, tmp_db):
        assert tmp_db.get_token_summary("nonexistent-run") == []

    def test_isolated_per_run(self, tmp_path):
        db = PipelineDB(tmp_path / "isolated.db")
        db.create_run("run-a", "a", {})
        db.create_run("run-b", "b", {})
        db.record_token_usage("run-a", "discovery", "domain_expert", 100, 50)
        db.record_token_usage("run-b", "discovery", "product_owner", 200, 80)
        assert len(db.get_token_summary("run-a")) == 1
        assert len(db.get_token_summary("run-b")) == 1

    def test_schema_migration_creates_token_usage_table(self, tmp_path):
        """A DB initialised with SCHEMA_VERSION=1 should migrate on next open."""
        import sqlite3
        # Create a v1 DB manually
        db_path = tmp_path / "v1.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO schema_meta VALUES ('version', '1')")
        conn.execute("""CREATE TABLE pipeline_runs (
            run_id TEXT PRIMARY KEY, idea TEXT NOT NULL,
            config_json TEXT, status TEXT, current_stage TEXT,
            started_at TEXT, completed_at TEXT)""")
        conn.commit()
        conn.close()

        # Open with new PipelineDB — should migrate
        db = PipelineDB(db_path)
        # Should be able to record without error
        db.create_run("run-mig", "test", {})
        db.record_token_usage("run-mig", "discovery", "domain_expert", 10, 5)
        rows = db.get_token_summary("run-mig")
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# cmd_deliver
# ---------------------------------------------------------------------------

class TestCmdDeliver:
    def _make_completed_run(self, tmp_path, db):
        """Set up a completed run with a delivery artifact."""
        db.update_run_status("run-1", "completed")
        db.save_artifact("run-1", "delivery", {
            "release_manager": {
                "version": "1.2.3",
                "release_tag": "myapp:1.2.3",
                "release_notes": "Initial release",
                "deployment_checklist": ["Check DNS", "Rotate secrets"],
                "rollback_procedure": "docker pull myapp:1.2.2",
            }
        })
        db.approve_artifact("run-1", "delivery", "test")

    def test_fails_for_nonexistent_run(self, tmp_path, tmp_db):
        from sdlc import cmd_deliver
        with patch("core.session.get_default_db", return_value=tmp_db):
            args = Namespace(run_id="nonexistent", no_docker=True, smoke_test=False)
            rc = cmd_deliver(args)
        assert rc == 1

    def test_fails_when_run_not_completed(self, tmp_path, tmp_db):
        from sdlc import cmd_deliver
        # run-1 is still "running"
        with patch("core.session.get_default_db", return_value=tmp_db):
            args = Namespace(run_id="run-1", no_docker=True, smoke_test=False)
            rc = cmd_deliver(args)
        assert rc == 1

    def test_no_docker_prints_checklist(self, tmp_path, tmp_db, capsys):
        from sdlc import cmd_deliver
        self._make_completed_run(tmp_path, tmp_db)

        with patch("core.session.get_default_db", return_value=tmp_db):
            args = Namespace(run_id="run-1", no_docker=True, smoke_test=False)
            rc = cmd_deliver(args)

        assert rc == 0
        out = capsys.readouterr().out
        assert "1.2.3" in out
        assert "Check DNS" in out

    def test_docker_build_when_dockerfile_present(self, tmp_path, tmp_db, capsys):
        from sdlc import cmd_deliver
        self._make_completed_run(tmp_path, tmp_db)

        # Create a fake workspace with Dockerfile
        workspace = tmp_path / "output" / "run-1"
        workspace.mkdir(parents=True)
        (workspace / "Dockerfile").write_text("FROM python:3.12-slim\n")

        proc = MagicMock(returncode=0, stdout="Successfully built abc\n", stderr="")
        with patch("core.session.get_default_db", return_value=tmp_db), \
             patch("subprocess.run", return_value=proc):
            args = Namespace(run_id="run-1", no_docker=False, smoke_test=False)
            rc = cmd_deliver(args)

        assert rc == 0
        out = capsys.readouterr().out
        assert "succeeded" in out.lower() or "myapp:1.2.3" in out

    def test_docker_build_failure_returns_1(self, tmp_path, tmp_db):
        from sdlc import cmd_deliver
        self._make_completed_run(tmp_path, tmp_db)

        workspace = tmp_path / "output" / "run-1"
        workspace.mkdir(parents=True)
        (workspace / "Dockerfile").write_text("FROM python:3.12-slim\n")

        proc = MagicMock(returncode=1, stdout="", stderr="Build failed: syntax error")
        with patch("core.session.get_default_db", return_value=tmp_db), \
             patch("subprocess.run", return_value=proc):
            args = Namespace(run_id="run-1", no_docker=False, smoke_test=False)
            rc = cmd_deliver(args)

        assert rc == 1


# ---------------------------------------------------------------------------
# cmd_cost
# ---------------------------------------------------------------------------

class TestCmdCost:
    def test_no_data_prints_message(self, tmp_db, capsys):
        from sdlc import cmd_cost
        with patch("core.session.get_default_db", return_value=tmp_db):
            rc = cmd_cost(Namespace(run_id="run-1"))
        assert rc == 0
        assert "No token usage" in capsys.readouterr().out

    def test_prints_cost_table(self, tmp_db, capsys):
        from sdlc import cmd_cost
        tmp_db.record_token_usage(
            "run-1", "discovery", "domain_expert",
            tokens_in=100_000, tokens_out=50_000, model="claude-sonnet-4-6",
        )
        tmp_db.record_token_usage(
            "run-1", "requirements", "business_analyst",
            tokens_in=200_000, tokens_out=80_000, model="claude-sonnet-4-6",
        )
        with patch("core.session.get_default_db", return_value=tmp_db):
            rc = cmd_cost(Namespace(run_id="run-1"))
        assert rc == 0
        out = capsys.readouterr().out
        assert "discovery" in out
        assert "requirements" in out
        assert "TOTAL" in out

    def test_cost_calculation_correctness(self, tmp_db, capsys):
        from sdlc import cmd_cost, _calc_cost
        # 1M tokens in + 1M tokens out at sonnet pricing (3.0 + 15.0 = $18.00)
        tmp_db.record_token_usage(
            "run-1", "discovery", "domain_expert",
            tokens_in=1_000_000, tokens_out=1_000_000, model="claude-sonnet-4-6",
        )
        expected = _calc_cost(1_000_000, 1_000_000, "claude-sonnet-4-6")
        assert abs(expected - 18.0) < 0.01

        with patch("core.session.get_default_db", return_value=tmp_db):
            cmd_cost(Namespace(run_id="run-1"))
        out = capsys.readouterr().out
        assert "18.0" in out

    def test_unknown_model_uses_default_pricing(self):
        from sdlc import _calc_cost
        cost = _calc_cost(1_000_000, 1_000_000, "unknown-model-xyz")
        default_cost = _calc_cost(1_000_000, 1_000_000, "claude-sonnet-4-6")
        assert cost == default_cost


# ---------------------------------------------------------------------------
# --dry-run flag
# ---------------------------------------------------------------------------

class TestDryRunFlag:
    def test_dry_run_prints_stage_plan(self, tmp_path, capsys):
        from sdlc import cmd_run
        args = Namespace(
            idea="Build a todo app",
            config=None, model=None, workspace=None,
            auto_approve=False, dry_run=True,
        )
        rc = cmd_run(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "discovery" in out
        assert "requirements" in out
        assert "quality_gate" in out
        assert "delivery" in out

    def test_dry_run_shows_hitl_gates(self, tmp_path, capsys):
        from sdlc import cmd_run
        args = Namespace(
            idea="Build a todo app",
            config=None, model=None, workspace=None,
            auto_approve=False, dry_run=True,
        )
        cmd_run(args)
        out = capsys.readouterr().out
        assert "HITL" in out

    @patch("core.agent_runtime.call_model_openai")
    def test_dry_run_makes_no_api_calls(self, mock_model, tmp_path):
        from sdlc import cmd_run
        args = Namespace(
            idea="Build a todo app",
            config=None, model=None, workspace=None,
            auto_approve=False, dry_run=True,
        )
        cmd_run(args)
        mock_model.assert_not_called()

    def test_dry_run_shows_model_info(self, capsys):
        from sdlc import cmd_run
        args = Namespace(
            idea="test", config=None,
            model="claude-opus-4-8", workspace=None,
            auto_approve=False, dry_run=True,
        )
        cmd_run(args)
        out = capsys.readouterr().out
        assert "claude-opus-4-8" in out
