"""Terminal command guardrail: allowlist, deny list, inline code, path arguments, override file."""

from __future__ import annotations

import json
import os
import shlex

import pytest

from tools import command_policy as cp
from tools._context import set_tool_context


@pytest.fixture
def ws(tmp_path):
    """A workspace the policy checks resolve paths against."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    set_tool_context(workspace=workspace, db=None, run_id="test-run")
    return workspace


def _check(command: str, cwd_rel: str = "."):
    return cp.check(shlex.split(command), cwd_rel)


class TestAllowlist:
    def test_default_list_has_35_commands(self):
        assert len(cp.DEFAULT_ALLOWED) == 35

    def test_default_and_deny_lists_do_not_overlap(self):
        assert not cp.DEFAULT_ALLOWED & cp.DENIED

    @pytest.mark.parametrize("name", sorted(cp.DEFAULT_ALLOWED))
    def test_every_default_command_is_allowed(self, name, ws):
        assert _check(name) is None

    @pytest.mark.parametrize("name", sorted(cp.DENIED))
    def test_denied_commands_are_blocked(self, name, ws):
        reason = _check(f"{name} anything")
        assert reason is not None and "blocked" in reason

    @pytest.mark.parametrize(
        "command", ["/bin/bash -c ls", "/usr/bin/curl x", "./sh", "venv/bin/bash", "../../bin/rm x"]
    )
    def test_denied_commands_are_blocked_by_basename(self, command, ws):
        assert _check(command) is not None

    @pytest.mark.parametrize("name", ["docker", "chmod", "perl", "__this_command_does_not_exist__"])
    def test_unlisted_commands_are_refused_with_the_fix(self, name, ws):
        reason = _check(name)
        assert "not on the terminal allowlist" in reason
        assert "terminal_allowlist.yaml" in reason  # tells the agent how to get it permitted

    def test_versioned_python_follows_python3(self, ws):
        assert _check("python3.12 script.py") is None
        assert _check("python3.12 -c 'print(1)'") is not None

    def test_absolute_command_path_is_fine_when_the_name_is_allowed(self, ws):
        assert _check("/usr/bin/python3 script.py") is None

    def test_empty_command(self):
        assert cp.check([]) == "Empty command"


class TestInlineCode:
    @pytest.mark.parametrize(
        "command",
        [
            "python -c 'print(1)'",
            "python3 -c 'print(1)'",
            "python -Sc 'print(1)'",
            "python -Bc 'print(1)'",
            "python -u -c 'print(1)'",
            "python -W ignore -c 'print(1)'",
            "python -Wignore -c 'print(1)'",
            "node -e 'console.log(1)'",
            "node --eval 'console.log(1)'",
            "node --eval=1",
            "node -p 1",
            "node --print 1",
            "node -pe 1",
            "node -r ./setup.js -e 'x'",
        ],
    )
    def test_inline_code_is_refused(self, command, ws):
        reason = _check(command)
        assert reason is not None and "inline code" in reason

    @pytest.mark.parametrize(
        "command",
        [
            "python script.py",
            "python script.py -c config.ini",  # -c belongs to the script
            "python -m pytest -c pytest.ini",  # -c belongs to pytest
            "python -u script.py",
            "python -W ignore script.py",
            "python -",
            "node app.js",
            "node app.js -e production",  # -e belongs to the script
            "node --inspect app.js",
        ],
    )
    def test_script_and_module_runs_are_fine(self, command, ws):
        assert _check(command) is None


class TestPathArguments:
    @pytest.mark.parametrize(
        "command",
        [
            "cat /etc/hosts",
            "cat ~/notes.txt",
            "cat ../secret",
            "cat a/../../secret",
            "cp notes.txt /etc/notes.txt",
            "mv notes.txt ../moved.txt",
            "head ~/.ssh/id_rsa",
            "tail /var/log/system.log",
            "find / -name x",
            "find .. -name x",
            "ls /",
            "mkdir /tmp/outside",
            "touch ../outside",
            "grep -r token /etc",
            "diff a.txt /etc/passwd",
            "git -C /somewhere status",
            "pytest --rootdir=/abs",
            "grep --file=/etc/passwd x",
        ],
    )
    def test_absolute_home_and_parent_paths_are_refused(self, command, ws):
        reason = _check(command)
        assert reason is not None
        assert "path" in reason.lower()

    @pytest.mark.parametrize(
        "command",
        [
            "ls",
            "ls -la",
            "ls src/tests",
            "cat README.md",
            "cat a..b",  # '..' only counts as a whole path component
            "cp a.txt b.txt",
            "mkdir -p build/out",
            "git log --oneline -n 5",
            "git add .",
            "grep -rn pattern .",
            "echo hello world",
            "sleep 1",
            "pytest tests/ -x --tb=short",
            "pytest --rootdir=sub",
            "uv sync --extra dev",
        ],
    )
    def test_relative_paths_inside_the_workspace_are_fine(self, command, ws):
        assert _check(command) is None

    def test_symlink_leading_out_of_the_workspace_is_refused(self, ws, tmp_path):
        outside = tmp_path / "outside.txt"
        outside.write_text("secret")
        (ws / "link").symlink_to(outside)
        reason = _check("cat link")
        assert reason is not None and "outside the workspace" in reason

    def test_symlink_staying_inside_is_fine(self, ws):
        (ws / "real.txt").write_text("x")
        (ws / "link").symlink_to(ws / "real.txt")
        assert _check("cat link") is None

    def test_paths_are_checked_relative_to_the_working_directory(self, ws, tmp_path):
        outside = tmp_path / "outside.txt"
        outside.write_text("secret")
        sub = ws / "sub"
        sub.mkdir()
        (sub / "link").symlink_to(outside)
        assert _check("cat link", cwd_rel="sub") is not None
        assert _check("cat other", cwd_rel="sub") is None

    def test_unusable_path_text_is_not_an_error(self, ws):
        assert _check("echo 'a\x00b'") is None  # embedded NUL cannot be a path; just text


class TestDocsMatchCode:
    """SECURITY.md lists the allowed and denied commands; the lists must not drift apart."""

    @staticmethod
    def _names(start: str, end: str) -> set:
        import re
        from pathlib import Path

        text = (Path(__file__).resolve().parent.parent / "SECURITY.md").read_text(encoding="utf-8")
        chunk = text.split(start, 1)[1].split(end, 1)[0]
        return set(re.findall(r"`([^`]+)`", chunk))

    def test_allowlist_in_security_md_matches_the_code(self):
        assert self._names("**Allowlist.**", "The match is on") == set(cp.DEFAULT_ALLOWED)

    def test_deny_list_in_security_md_matches_the_code(self):
        assert self._names("**Hard deny list.**", "are refused") == set(cp.DENIED)


class TestOverrideFile:
    def _write(self, tmp_path, text):
        path = tmp_path / "policy.yaml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_missing_file_means_the_defaults(self, tmp_path):
        policy = cp.load_policy(tmp_path / "nope.yaml")
        assert policy.allowed == cp.DEFAULT_ALLOWED and policy.error is None

    def test_empty_file_means_the_defaults(self, tmp_path):
        policy = cp.load_policy(self._write(tmp_path, ""))
        assert policy.allowed == cp.DEFAULT_ALLOWED and policy.error is None

    def test_allow_adds_commands(self, tmp_path):
        policy = cp.load_policy(self._write(tmp_path, "allow: [docker, chmod]\n"))
        assert policy.allowed == cp.DEFAULT_ALLOWED | {"docker", "chmod"}

    def test_remove_takes_commands_away(self, tmp_path):
        policy = cp.load_policy(self._write(tmp_path, "remove: [cp, git]\n"))
        assert policy.allowed == cp.DEFAULT_ALLOWED - {"cp", "git"}

    @pytest.mark.parametrize("name", sorted(cp.DENIED))
    def test_denied_commands_can_never_be_allowed(self, name, tmp_path):
        policy = cp.load_policy(self._write(tmp_path, f"allow: [{name}]\n"))
        assert policy.error is not None and "can never be allowed" in policy.error
        assert policy.allowed == frozenset()

    @pytest.mark.parametrize(
        "text",
        [
            "allow: [unterminated\n",  # malformed YAML
            "just a string\n",
            "- a\n- list\n",
            "allow: docker\n",  # not a list
            "allow: [1, 2]\n",  # not names
            "allow: ['with space']\n",
            "allow: ['/bin/ls']\n",
            "deny: [ls]\n",  # unknown key
            "allow: [docker]\nextra: 1\n",
        ],
    )
    def test_invalid_file_fails_closed(self, text, tmp_path):
        path = self._write(tmp_path, text)
        policy = cp.load_policy(path)
        assert policy.error is not None and str(path) in policy.error
        assert policy.allowed == frozenset()

    def test_unreadable_file_fails_closed(self, tmp_path):
        path = tmp_path / "policy.yaml"
        path.write_bytes(b"\xff\xfe\x00 not utf-8 \x80")
        assert cp.load_policy(path).error is not None

    def test_failed_closed_policy_refuses_every_command(self, tmp_path, monkeypatch, ws):
        path = self._write(tmp_path, "allow: [unterminated\n")
        monkeypatch.setattr(cp, "policy_path", lambda: path)
        cp.reset_policy_cache()
        for command in ("ls", "echo hi", "python script.py"):
            reason = _check(command)
            assert reason is not None and str(path) in reason

    def test_policy_is_loaded_once_per_process(self, tmp_path, monkeypatch, ws):
        path = self._write(tmp_path, "allow: [docker]\n")
        monkeypatch.setattr(cp, "policy_path", lambda: path)
        cp.reset_policy_cache()
        assert _check("docker ps") is None
        path.write_text("remove: [docker]\nallow: []\n")  # changed mid-run: must not take effect
        assert _check("docker ps") is None
        cp.reset_policy_cache()
        assert _check("docker ps") is not None

    def test_override_never_touches_the_workspace_copy(self, tmp_path, monkeypatch, ws):
        """An agent can write inside its workspace; only the home-directory file counts."""
        (ws / "terminal_allowlist.yaml").write_text("allow: [docker]\n")
        cp.reset_policy_cache()
        assert _check("docker ps", cwd_rel=".") is not None


class TestTerminalToolIntegration:
    def test_refused_command_never_reaches_a_subprocess(self, ws, monkeypatch):
        from tools import terminal_tool

        def boom(*a, **kw):  # pragma: no cover - must not be called
            raise AssertionError("subprocess.run was called for a refused command")

        monkeypatch.setattr(terminal_tool.subprocess, "run", boom)
        for command in (
            "bash -c 'echo hi'",
            "curl http://example.com",
            "python3 -c 'print(1)'",
            "cat /etc/hosts",
            "rm -rf .",
        ):
            result = json.loads(terminal_tool._terminal(command))
            assert "error" in result, command

    def test_refusal_message_reaches_the_agent(self, ws):
        from tools.terminal_tool import _terminal

        result = json.loads(_terminal("bash -c 'echo hi'"))
        assert "blocked" in result["error"]

    def test_allowed_command_still_runs(self, ws):
        from tools.terminal_tool import _terminal

        result = json.loads(_terminal("echo hello"))
        assert result["ok"] is True and "hello" in result["stdout"]

    def test_path_check_follows_the_working_directory(self, ws):
        from tools.terminal_tool import _terminal

        (ws / "sub").mkdir()
        (ws / "sub" / "f.txt").write_text("hi")
        ok = json.loads(_terminal("cat f.txt", cwd="sub"))
        assert ok["ok"] is True
        refused = json.loads(_terminal("cat ../f.txt", cwd="sub"))
        assert "error" in refused

    def test_environment_is_unchanged_by_refusals(self, ws, monkeypatch):
        monkeypatch.setenv("SDLC_POLICY_PROBE", "kept")
        from tools.terminal_tool import _terminal

        _terminal("bash -c 'x'")
        assert os.environ["SDLC_POLICY_PROBE"] == "kept"
