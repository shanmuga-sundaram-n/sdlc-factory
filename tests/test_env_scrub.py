"""Agent-run subprocesses must not inherit the dashboard token or provider secrets."""

from __future__ import annotations

import json
import sys

import pytest

from tools._context import SECRET_ENV_VARS, agent_env, set_tool_context


@pytest.mark.parametrize("name", SECRET_ENV_VARS)
def test_agent_env_drops_each_secret(name, monkeypatch):
    monkeypatch.setenv(name, "s3cret")
    assert name not in agent_env()


def test_agent_env_keeps_everything_else(monkeypatch):
    monkeypatch.setenv("SDLC_TEST_KEEP", "1")
    env = agent_env()
    assert env["SDLC_TEST_KEEP"] == "1"
    assert "PATH" in env


def test_agent_env_does_not_mutate_the_real_environment(monkeypatch):
    import os

    monkeypatch.setenv("SDLC_UI_TOKEN", "still-here")
    agent_env()
    assert os.environ["SDLC_UI_TOKEN"] == "still-here"


def test_terminal_children_cannot_read_secrets(tmp_path, monkeypatch):
    for name in SECRET_ENV_VARS:
        monkeypatch.setenv(name, "s3cret")
    set_tool_context(workspace=tmp_path, db=None, run_id="test-run")
    (tmp_path / "probe.py").write_text(
        "import json, os\n"
        f"print(json.dumps({{k: os.environ.get(k) for k in {list(SECRET_ENV_VARS)!r}}}))\n"
    )
    from tools.terminal_tool import _terminal

    result = json.loads(_terminal(f"{sys.executable} probe.py"))
    assert result["ok"] is True, result
    seen = json.loads(result["stdout"])
    assert set(seen) == set(SECRET_ENV_VARS)
    assert all(value is None for value in seen.values()), seen


def test_pytest_tool_passes_a_scrubbed_env(tmp_path, monkeypatch):
    for name in SECRET_ENV_VARS:
        monkeypatch.setenv(name, "s3cret")
    set_tool_context(workspace=tmp_path, db=None, run_id="test-run")
    captured = {}

    class _Proc:
        stdout = ""
        stderr = ""
        returncode = 0

    def fake_run(cmd, **kwargs):
        captured.update(kwargs)
        return _Proc()

    monkeypatch.setattr("tools.pytest_tool.subprocess.run", fake_run)
    from tools.pytest_tool import run_pytest

    run_pytest()
    assert "env" in captured
    assert not set(SECRET_ENV_VARS) & set(captured["env"])
