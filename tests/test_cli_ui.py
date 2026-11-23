"""`sdlc.py ui`: per-launch token, link output, loopback guard."""

from __future__ import annotations

import argparse
import os

import pytest

import sdlc

TOKEN_VARS = ("SDLC_UI_TOKEN", "SDLC_UI_HOST", "SDLC_UI_ALLOW_REMOTE")


def _args(**overrides) -> argparse.Namespace:
    values = {"host": "127.0.0.1", "port": 7474, "reload": False, "allow_remote": False}
    values.update(overrides)
    return argparse.Namespace(**values)


@pytest.fixture(autouse=True)
def fake_uvicorn(monkeypatch):
    """Record uvicorn.run calls; seed the env vars so monkeypatch restores them."""
    for name in TOKEN_VARS:
        monkeypatch.setenv(name, "placeholder")
    calls = []
    monkeypatch.setattr("uvicorn.run", lambda *a, **kw: calls.append((a, kw)))
    return calls


class TestLaunch:
    def test_prints_link_alone_on_its_own_line(self, capsys, fake_uvicorn):
        assert sdlc.cmd_ui(_args()) == 0
        lines = capsys.readouterr().out.splitlines()
        token = os.environ["SDLC_UI_TOKEN"]
        links = [line for line in lines if line.startswith("http://")]
        assert links == [f"http://127.0.0.1:7474/#token={token}"]
        idx = lines.index(links[0])
        assert "includes your access key" in lines[idx - 1]
        assert "do not share it" in lines[idx + 1]
        assert "Restarting makes a new link" in lines[idx + 1]

    def test_token_is_long_random_and_new_each_launch(self, fake_uvicorn):
        sdlc.cmd_ui(_args())
        first = os.environ["SDLC_UI_TOKEN"]
        sdlc.cmd_ui(_args())
        second = os.environ["SDLC_UI_TOKEN"]
        assert len(first) >= 40 and first != second and first != "placeholder"

    def test_starts_uvicorn_by_import_string_with_bind_settings(self, fake_uvicorn):
        sdlc.cmd_ui(_args(port=9000, reload=True))
        ((args, kwargs),) = fake_uvicorn
        assert args == ("gateway.web_server:app",)
        assert kwargs["host"] == "127.0.0.1" and kwargs["port"] == 9000 and kwargs["reload"] is True
        assert os.environ["SDLC_UI_HOST"] == "127.0.0.1"

    def test_token_is_not_in_stderr(self, capsys, fake_uvicorn):
        sdlc.cmd_ui(_args())
        captured = capsys.readouterr()
        assert os.environ["SDLC_UI_TOKEN"] not in captured.err

    @pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "localhost", "::1"])
    def test_loopback_hosts_start(self, host, fake_uvicorn):
        assert sdlc.cmd_ui(_args(host=host)) == 0
        assert len(fake_uvicorn) == 1


class TestRemoteGuard:
    @pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "example.com", "::"])
    def test_non_loopback_refused_without_flag(self, host, capsys, fake_uvicorn):
        assert sdlc.cmd_ui(_args(host=host)) == 1
        err = capsys.readouterr().err
        assert "--allow-remote" in err
        assert fake_uvicorn == []
        # No token was minted for a launch that never happened.
        assert os.environ["SDLC_UI_TOKEN"] == "placeholder"

    def test_allow_remote_starts_and_links_to_loopback(self, capsys, fake_uvicorn):
        assert sdlc.cmd_ui(_args(host="0.0.0.0", allow_remote=True)) == 0
        out = capsys.readouterr().out
        assert os.environ["SDLC_UI_ALLOW_REMOTE"] == "1"
        assert "http://127.0.0.1:7474/#token=" in out
        assert len(fake_uvicorn) == 1

    def test_allow_remote_flag_is_parsed(self):
        parser_args = sdlc.build_parser().parse_args(["ui", "--allow-remote"])
        assert parser_args.allow_remote is True
