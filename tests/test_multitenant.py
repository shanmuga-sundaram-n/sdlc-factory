"""Multi-tenant isolation tests.

Verifies that:
- Runs created under tenant A are invisible to tenant B
- list_runs is scoped by tenant
- get_run enforces tenant boundary
- update_run_status only affects the correct tenant's run
- TenantConfig loads from .env files and falls back to os.environ
"""

from __future__ import annotations

import os
import textwrap


from core.session import PipelineDB
from core.tenant import TenantConfig


# ---------------------------------------------------------------------------
# DB isolation helpers
# ---------------------------------------------------------------------------

class TestTenantDBIsolation:
    def test_runs_scoped_by_tenant(self, tmp_path):
        db_path = tmp_path / "pipeline.db"
        db_a = PipelineDB(db_path, tenant_id="acme")
        db_b = PipelineDB(db_path, tenant_id="globex")

        db_a.create_run("run-a1", "idea A", {})
        db_b.create_run("run-b1", "idea B", {})

        runs_a = db_a.list_runs()
        runs_b = db_b.list_runs()

        assert len(runs_a) == 1
        assert runs_a[0]["run_id"] == "run-a1"
        assert len(runs_b) == 1
        assert runs_b[0]["run_id"] == "run-b1"

    def test_get_run_enforces_tenant(self, tmp_path):
        db_path = tmp_path / "pipeline.db"
        db_a = PipelineDB(db_path, tenant_id="acme")
        db_b = PipelineDB(db_path, tenant_id="globex")

        db_a.create_run("run-a1", "idea A", {})

        # Tenant A can read its own run
        assert db_a.get_run("run-a1") is not None
        # Tenant B cannot see tenant A's run
        assert db_b.get_run("run-a1") is None

    def test_update_run_status_scoped(self, tmp_path):
        db_path = tmp_path / "pipeline.db"
        db_a = PipelineDB(db_path, tenant_id="acme")
        db_b = PipelineDB(db_path, tenant_id="globex")

        db_a.create_run("run-a1", "idea A", {})
        # Tenant B tries to update tenant A's run — should be a no-op
        db_b.update_run_status("run-a1", "completed")

        # Tenant A's run is unmodified
        run = db_a.get_run("run-a1")
        assert run is not None
        assert run["status"] == "running"

    def test_default_tenant_isolated(self, tmp_path):
        db_path = tmp_path / "pipeline.db"
        db_default = PipelineDB(db_path)          # tenant_id="default"
        db_other   = PipelineDB(db_path, tenant_id="other")

        db_default.create_run("run-d1", "idea default", {})
        db_other.create_run("run-o1", "idea other", {})

        assert len(db_default.list_runs()) == 1
        assert db_default.list_runs()[0]["run_id"] == "run-d1"
        assert len(db_other.list_runs()) == 1
        assert db_other.list_runs()[0]["run_id"] == "run-o1"

    def test_artifacts_accessible_cross_tenant_by_run_id(self, tmp_path):
        """Artifact queries are by run_id (UUID) which is already unique — no tenant
        filtering needed at the artifact layer. Just verify artifacts persist normally."""
        db_path = tmp_path / "pipeline.db"
        db = PipelineDB(db_path, tenant_id="acme")
        db.create_run("run-x1", "idea X", {})
        db.save_artifact("run-x1", "discovery", {"result": "ok"})
        art = db.get_artifact("run-x1", "discovery")
        assert art is not None
        assert art["result"] == "ok"

    def test_schema_version_is_3(self, tmp_path):
        from core.session import SCHEMA_VERSION
        assert SCHEMA_VERSION == 3

    def test_new_db_has_tenant_id_column(self, tmp_path):
        db = PipelineDB(tmp_path / "pipeline.db")
        db.create_run("run-v3", "test", {})
        row = db._conn().execute(
            "SELECT tenant_id FROM pipeline_runs WHERE run_id='run-v3'"
        ).fetchone()
        assert row is not None
        assert row["tenant_id"] == "default"

    def test_multiple_runs_same_tenant(self, tmp_path):
        db = PipelineDB(tmp_path / "pipeline.db", tenant_id="bigcorp")
        for i in range(5):
            db.create_run(f"run-{i}", f"idea {i}", {})
        assert len(db.list_runs()) == 5


# ---------------------------------------------------------------------------
# TenantConfig
# ---------------------------------------------------------------------------

class TestTenantConfig:
    def test_load_from_env_file(self, tmp_path):
        env_file = tmp_path / "acme.env"
        env_file.write_text(
            "OPENAI_API_KEY=sk-acme-123\nOPENAI_BASE_URL=https://acme.example.com\n",
            encoding="utf-8",
        )
        tc = TenantConfig.load("acme", tenant_dir=tmp_path)
        assert tc.api_key == "sk-acme-123"
        assert tc.base_url == "https://acme.example.com"

    def test_missing_file_returns_empty_config(self, tmp_path):
        tc = TenantConfig.load("nonexistent", tenant_dir=tmp_path)
        # Should not raise; api_key falls back to os.environ
        assert tc.tenant_id == "nonexistent"

    def test_comments_and_blank_lines_skipped(self, tmp_path):
        env_file = tmp_path / "test.env"
        env_file.write_text(
            textwrap.dedent("""\
                # This is a comment
                OPENAI_API_KEY=sk-test

                # Another comment
                SOME_VAR=hello
            """),
            encoding="utf-8",
        )
        tc = TenantConfig.load("test", tenant_dir=tmp_path)
        assert tc.get("OPENAI_API_KEY") == "sk-test"
        assert tc.get("SOME_VAR") == "hello"

    def test_get_falls_back_to_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FALLBACK_KEY", "from-env")
        tc = TenantConfig.load("empty_tenant", tenant_dir=tmp_path)
        assert tc.get("FALLBACK_KEY") == "from-env"

    def test_apply_to_env_sets_os_environ(self, tmp_path, monkeypatch):
        env_file = tmp_path / "test.env"
        env_file.write_text("MY_CUSTOM_VAR=injected\n", encoding="utf-8")
        tc = TenantConfig.load("test", tenant_dir=tmp_path)
        # Make sure the key is not already set
        monkeypatch.delenv("MY_CUSTOM_VAR", raising=False)
        tc.apply_to_env()
        assert os.environ.get("MY_CUSTOM_VAR") == "injected"

    def test_repr_includes_tenant_id(self, tmp_path):
        tc = TenantConfig.load("myco", tenant_dir=tmp_path)
        assert "myco" in repr(tc)

    def test_get_with_default(self, tmp_path):
        tc = TenantConfig.load("empty", tenant_dir=tmp_path)
        assert tc.get("NONEXISTENT_KEY", "fallback") == "fallback"
