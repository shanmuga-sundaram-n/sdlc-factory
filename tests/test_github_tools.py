"""GitHub tool tests.

All `gh` CLI invocations are mocked via subprocess.run; no real GitHub
authentication or network is required.
"""

from __future__ import annotations

import json
from unittest.mock import patch, MagicMock

# Ensure tools module is importable
import tools._context  # noqa: F401


def _mock_run(stdout="", stderr="", returncode=0):
    """Return a mock that replaces subprocess.run."""
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


def _patch_gh(stdout="", stderr="", returncode=0):
    return patch(
        "tools.github_tool.subprocess.run",
        return_value=_mock_run(stdout, stderr, returncode),
    )


def _patch_workspace():
    return patch(
        "tools.github_tool.safe_resolve", return_value=MagicMock(__str__=lambda s: "/tmp/workspace")
    )


class TestGhListPrs:
    def test_returns_prs(self):
        prs_json = json.dumps(
            [
                {
                    "number": 1,
                    "title": "feat: add login",
                    "state": "open",
                    "url": "https://github.com/org/repo/pull/1",
                }
            ]
        )
        with _patch_gh(stdout=prs_json), _patch_workspace():
            from tools.github_tool import _gh_list_prs

            result = json.loads(_gh_list_prs({}))
        assert result["count"] == 1
        assert result["prs"][0]["number"] == 1

    def test_empty_list(self):
        with _patch_gh(stdout="[]"), _patch_workspace():
            from tools.github_tool import _gh_list_prs

            result = json.loads(_gh_list_prs({"state": "closed"}))
        assert result["prs"] == []

    def test_gh_not_found(self):
        with (
            patch("tools.github_tool.subprocess.run", side_effect=FileNotFoundError),
            _patch_workspace(),
        ):
            from tools.github_tool import _gh_list_prs

            result = json.loads(_gh_list_prs({}))
        assert "error" in result
        assert "gh CLI not found" in result["error"]

    def test_gh_returns_error(self):
        with _patch_gh(stderr="not in a git repo", returncode=1), _patch_workspace():
            from tools.github_tool import _gh_list_prs

            result = json.loads(_gh_list_prs({}))
        assert "error" in result


class TestGhCreatePr:
    def test_success(self):
        pr_url = "https://github.com/org/repo/pull/42"
        with _patch_gh(stdout=pr_url), _patch_workspace():
            from tools.github_tool import _gh_create_pr

            result = json.loads(
                _gh_create_pr({"title": "feat: new feature", "body": "Description"})
            )
        assert result["success"] is True
        assert result["url"] == pr_url

    def test_missing_title(self):
        with _patch_workspace():
            from tools.github_tool import _gh_create_pr

            result = json.loads(_gh_create_pr({}))
        assert "error" in result

    def test_draft_pr(self):
        captured = {}

        def _capture(*args, **kwargs):
            captured["args"] = args[0]
            return _mock_run(stdout="https://github.com/org/repo/pull/99")

        with patch("tools.github_tool.subprocess.run", side_effect=_capture), _patch_workspace():
            from tools.github_tool import _gh_create_pr

            _gh_create_pr({"title": "WIP: something", "draft": True})

        assert "--draft" in captured["args"]

    def test_gh_failure(self):
        with _patch_gh(stderr="remote: permission denied", returncode=1), _patch_workspace():
            from tools.github_tool import _gh_create_pr

            result = json.loads(_gh_create_pr({"title": "feat: x"}))
        assert "error" in result


class TestGhRequestReview:
    def test_adds_reviewer(self):
        with _patch_gh(stdout=""), _patch_workspace():
            from tools.github_tool import _gh_request_review

            result = json.loads(_gh_request_review({"pr_number": 1, "reviewers": ["alice"]}))
        assert result["success"] is True

    def test_missing_pr_number(self):
        with _patch_workspace():
            from tools.github_tool import _gh_request_review

            result = json.loads(_gh_request_review({"reviewers": ["alice"]}))
        assert "error" in result

    def test_missing_reviewers_and_teams(self):
        with _patch_workspace():
            from tools.github_tool import _gh_request_review

            result = json.loads(_gh_request_review({"pr_number": 1}))
        assert "error" in result

    def test_adds_team(self):
        captured = {}

        def _capture(*args, **kwargs):
            captured["args"] = args[0]
            return _mock_run()

        with patch("tools.github_tool.subprocess.run", side_effect=_capture), _patch_workspace():
            from tools.github_tool import _gh_request_review

            _gh_request_review({"pr_number": 5, "teams": ["org/backend"]})

        assert "org/backend" in captured["args"]


class TestGhMergePr:
    def test_squash_merge(self):
        with _patch_gh(), _patch_workspace():
            from tools.github_tool import _gh_merge_pr

            result = json.loads(_gh_merge_pr({"pr_number": 7}))
        assert result["success"] is True
        assert result["method"] == "squash"

    def test_merge_method(self):
        captured = {}

        def _capture(*args, **kwargs):
            captured["args"] = args[0]
            return _mock_run()

        with patch("tools.github_tool.subprocess.run", side_effect=_capture), _patch_workspace():
            from tools.github_tool import _gh_merge_pr

            _gh_merge_pr({"pr_number": 8, "method": "rebase"})

        assert "--rebase" in captured["args"]

    def test_invalid_method(self):
        with _patch_workspace():
            from tools.github_tool import _gh_merge_pr

            result = json.loads(_gh_merge_pr({"pr_number": 9, "method": "force"}))
        assert "error" in result

    def test_missing_pr_number(self):
        with _patch_workspace():
            from tools.github_tool import _gh_merge_pr

            result = json.loads(_gh_merge_pr({}))
        assert "error" in result

    def test_gh_failure(self):
        with _patch_gh(stderr="PR not mergeable", returncode=1), _patch_workspace():
            from tools.github_tool import _gh_merge_pr

            result = json.loads(_gh_merge_pr({"pr_number": 10}))
        assert "error" in result


class TestGithubToolRegistration:
    def test_all_tools_registered(self):
        from core.registry import registry
        import tools.github_tool  # noqa — ensure registered

        defs = registry.get_definitions(enabled_toolsets=["github"])
        names = {d["function"]["name"] for d in defs}
        assert "gh_list_prs" in names
        assert "gh_create_pr" in names
        assert "gh_request_review" in names
        assert "gh_merge_pr" in names

    def test_devops_agent_has_github_toolset(self):
        from agents.roles import DeveloperDevOpsAgent

        assert "github" in DeveloperDevOpsAgent.ENABLED_TOOLSETS
