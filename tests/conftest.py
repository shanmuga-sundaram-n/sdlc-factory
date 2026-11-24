"""Shared test fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_terminal_policy(tmp_path, monkeypatch):
    """Point the terminal policy at an empty temp location for every test.

    Without this, a developer's real ~/.sdlc-factory/terminal_allowlist.yaml would
    change which commands the terminal tool allows, and the policy is cached per
    process, so one test's file would leak into the next.
    """
    from tools import command_policy

    monkeypatch.setattr(command_policy, "policy_path", lambda: tmp_path / "terminal_allowlist.yaml")
    command_policy.reset_policy_cache()
    yield
    command_policy.reset_policy_cache()
