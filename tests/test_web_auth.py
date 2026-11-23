"""Dashboard API auth: every /api route needs the per-launch access token."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.session import PipelineDB

TOKEN = "test-token-123"
RUN = "run-1"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
REPO_ROOT = Path(__file__).resolve().parent.parent

READ_PATHS = [
    "/api/runs",
    f"/api/runs/{RUN}",
    f"/api/runs/{RUN}/stages",
    f"/api/runs/{RUN}/artifacts/requirements",
    f"/api/runs/{RUN}/kanban",
    f"/api/runs/{RUN}/audit",
    f"/api/runs/{RUN}/cost",
]
APPROVE_PATH = f"/api/runs/{RUN}/approve"


def _make_app(tmp_path, monkeypatch):
    from gateway import web_server

    db = PipelineDB(tmp_path / "test.db")
    db.create_run(RUN, "an idea", {})
    db.save_artifact(RUN, "requirements", {"summary": "x"})
    monkeypatch.setattr("core.session.get_default_db", lambda: db)
    return web_server._get_app(), db


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("SDLC_UI_TOKEN", TOKEN)
    monkeypatch.delenv("SDLC_UI_HOST", raising=False)
    monkeypatch.delenv("SDLC_UI_ALLOW_REMOTE", raising=False)


@pytest.fixture
def app_db(tmp_path, monkeypatch, env):
    return _make_app(tmp_path, monkeypatch)


@pytest.fixture
def client(app_db):
    app, _db = app_db
    # TestClient's default Host is "testserver", which the Host check rightly refuses.
    return TestClient(app, base_url="http://127.0.0.1")


class TestTokenRequired:
    @pytest.mark.parametrize("path", READ_PATHS)
    def test_read_endpoints_401_without_token(self, client, path):
        r = client.get(path)
        assert r.status_code == 401
        assert r.headers["www-authenticate"] == "Bearer"

    def test_approve_401_without_token(self, client):
        r = client.post(APPROVE_PATH, json={"stage": "requirements", "decision": "APPROVED"})
        assert r.status_code == 401

    def test_every_api_route_is_protected(self, app_db):
        """Walk the real route table so a new unprotected /api route fails here."""
        app, _db = app_db
        client = TestClient(app, base_url="http://127.0.0.1")
        checked = 0
        for route in app.routes:
            path = getattr(route, "path", "")
            if not path.startswith("/api/") or path.startswith("/api/docs"):
                continue
            url = path.replace("{run_id}", RUN).replace("{stage}", "requirements")
            for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
                kwargs = {"json": {}} if method == "POST" else {}
                r = client.request(method, url, **kwargs)
                assert (
                    r.status_code == 401
                ), f"{method} {path} answered {r.status_code} without a token"
                checked += 1
        assert checked == 8

    @pytest.mark.parametrize(
        "header",
        ["Bearer wrong-token", "Basic dGVzdDp0ZXN0", "Bearer", "", f"Token {TOKEN}", TOKEN],
    )
    def test_bad_credentials_401(self, client, header):
        r = client.get("/api/runs", headers={"Authorization": header})
        assert r.status_code == 401

    def test_fails_closed_when_no_token_configured(self, client, monkeypatch):
        monkeypatch.delenv("SDLC_UI_TOKEN")
        r = client.get("/api/runs", headers={"Authorization": "Bearer "})
        assert r.status_code == 401
        r = client.get("/api/runs", headers=AUTH)
        assert r.status_code == 401


class TestTokenAccepted:
    @pytest.mark.parametrize("path", READ_PATHS)
    def test_read_endpoints_200_with_token(self, client, path):
        assert client.get(path, headers=AUTH).status_code == 200

    def test_scheme_is_case_insensitive(self, client):
        r = client.get("/api/runs", headers={"Authorization": f"bearer {TOKEN}"})
        assert r.status_code == 200

    def test_approve_with_token(self, client):
        r = client.post(
            APPROVE_PATH,
            headers=AUTH,
            json={"stage": "requirements", "decision": "APPROVED", "rationale": "ok"},
        )
        assert r.status_code == 200
        assert r.json() == {"status": "approved"}

    def test_amend_with_token_overwrites_artifact(self, client, app_db):
        _app, db = app_db
        r = client.post(
            APPROVE_PATH,
            headers=AUTH,
            json={
                "stage": "requirements",
                "decision": "AMENDED",
                "rationale": "fix it",
                "amended_artifact": {"summary": "changed"},
            },
        )
        assert r.status_code == 200
        assert r.json()["status"] == "amended"
        assert db.get_artifact(RUN, "requirements") == {"summary": "changed"}

    def test_spa_shell_stays_public(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]


class TestHostAndCors:
    def test_foreign_host_header_rejected_even_with_token(self, client):
        r = client.get("/api/runs", headers={**AUTH, "Host": "evil.example"})
        assert r.status_code == 400

    def test_localhost_name_accepted(self, client):
        r = client.get("/api/runs", headers={**AUTH, "Host": "localhost:7474"})
        assert r.status_code == 200

    def test_bound_host_accepted(self, tmp_path, monkeypatch, env):
        monkeypatch.setenv("SDLC_UI_HOST", "dev-box.local")
        app, _ = _make_app(tmp_path, monkeypatch)
        c = TestClient(app, base_url="http://dev-box.local")
        assert c.get("/api/runs", headers=AUTH).status_code == 200

    def test_allow_remote_accepts_any_host_but_still_needs_token(self, tmp_path, monkeypatch, env):
        monkeypatch.setenv("SDLC_UI_ALLOW_REMOTE", "1")
        app, _ = _make_app(tmp_path, monkeypatch)
        c = TestClient(app, base_url="http://192.168.1.50")
        assert c.get("/api/runs", headers=AUTH).status_code == 200
        assert c.get("/api/runs").status_code == 401

    def test_no_cors_headers_for_foreign_origin(self, client):
        preflight = client.options(
            "/api/runs",
            headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
        )
        assert "access-control-allow-origin" not in preflight.headers
        r = client.get("/api/runs", headers={**AUTH, "Origin": "https://evil.example"})
        assert "access-control-allow-origin" not in r.headers


class TestDashboardPage:
    def test_every_api_call_goes_through_the_shared_request_function(self):
        """A raw fetch() would skip the token and the 401 lock screen, so only request() may call it."""
        html = (REPO_ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        assert html.count("fetch(") == 1
        assert "async function request(" in html


class TestStartup:
    def test_refuses_to_start_without_token(self, tmp_path, monkeypatch, env):
        monkeypatch.delenv("SDLC_UI_TOKEN")
        app, _ = _make_app(tmp_path, monkeypatch)
        with pytest.raises(RuntimeError, match="SDLC_UI_TOKEN"):
            with TestClient(app, base_url="http://127.0.0.1"):
                pass

    def test_starts_with_token(self, client):
        with client:
            assert client.get("/api/runs", headers=AUTH).status_code == 200

    def test_importing_the_module_does_not_need_the_token(self):
        """The refuse-to-start check lives in startup, so plain imports keep working."""
        clean_env = {k: v for k, v in os.environ.items() if k != "SDLC_UI_TOKEN"}
        proc = subprocess.run(
            [sys.executable, "-c", "import gateway.web_server"],
            cwd=REPO_ROOT,
            env=clean_env,
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr
