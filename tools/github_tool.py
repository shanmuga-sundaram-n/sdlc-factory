"""GitHub integration tools using the `gh` CLI — registered in the 'github' toolset.

Requires the GitHub CLI (https://cli.github.com) to be installed and authenticated
(`gh auth login`).  All commands are run as subprocess calls; no GitHub API key
is needed beyond what `gh` already manages.

Available tools:
  - gh_list_prs      — list open PRs in the current repo
  - gh_create_pr     — open a new pull request
  - gh_request_review — add reviewers to an existing PR
  - gh_merge_pr      — merge a PR (squash by default)
"""

from __future__ import annotations

import json
import subprocess
from typing import Any, Dict, List, Optional

from core.registry import registry
from tools._context import safe_resolve


def _run_gh(args: List[str], cwd: Optional[str] = None, timeout: int = 30) -> Dict[str, Any]:
    """Run a `gh` CLI command and return {returncode, stdout, stderr}."""
    try:
        result = subprocess.run(
            ["gh"] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "returncode": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        }
    except FileNotFoundError:
        return {
            "returncode": -1,
            "stdout": "",
            "stderr": "gh CLI not found — install from https://cli.github.com",
        }
    except subprocess.TimeoutExpired:
        return {
            "returncode": -1,
            "stdout": "",
            "stderr": f"gh command timed out after {timeout}s",
        }


def _gh_list_prs(args: dict) -> str:
    state = args.get("state", "open")
    limit = int(args.get("limit", 20))
    workspace = str(safe_resolve(""))

    result = _run_gh(
        ["pr", "list", "--state", state, "--limit", str(limit), "--json",
         "number,title,author,headRefName,state,url"],
        cwd=workspace,
    )
    if result["returncode"] != 0:
        return json.dumps({"error": result["stderr"] or "gh pr list failed"})

    try:
        prs = json.loads(result["stdout"]) if result["stdout"] else []
    except json.JSONDecodeError:
        prs = []
    return json.dumps({"prs": prs, "count": len(prs)})


def _gh_create_pr(args: dict) -> str:
    title = args.get("title", "")
    body = args.get("body", "")
    base = args.get("base", "main")
    head = args.get("head", "")
    draft = bool(args.get("draft", False))
    workspace = str(safe_resolve(""))

    if not title:
        return json.dumps({"error": "title is required"})

    cmd = ["pr", "create", "--title", title, "--body", body, "--base", base]
    if head:
        cmd += ["--head", head]
    if draft:
        cmd.append("--draft")

    result = _run_gh(cmd, cwd=workspace, timeout=60)
    if result["returncode"] != 0:
        return json.dumps({"error": result["stderr"] or "gh pr create failed"})

    return json.dumps({"success": True, "url": result["stdout"]})


def _gh_request_review(args: dict) -> str:
    pr_number = str(args.get("pr_number", ""))
    reviewers = args.get("reviewers", [])
    teams = args.get("teams", [])
    workspace = str(safe_resolve(""))

    if not pr_number:
        return json.dumps({"error": "pr_number is required"})
    if not reviewers and not teams:
        return json.dumps({"error": "At least one reviewer or team is required"})

    cmd = ["pr", "edit", pr_number]
    for r in reviewers:
        cmd += ["--add-reviewer", r]
    for t in teams:
        cmd += ["--add-reviewer", t]

    result = _run_gh(cmd, cwd=workspace)
    if result["returncode"] != 0:
        return json.dumps({"error": result["stderr"] or "gh pr edit failed"})

    return json.dumps({"success": True, "pr_number": pr_number})


def _gh_merge_pr(args: dict) -> str:
    pr_number = str(args.get("pr_number", ""))
    method = args.get("method", "squash")      # squash | merge | rebase
    delete_branch = bool(args.get("delete_branch", True))
    workspace = str(safe_resolve(""))

    if not pr_number:
        return json.dumps({"error": "pr_number is required"})
    if method not in ("squash", "merge", "rebase"):
        return json.dumps({"error": f"method must be squash, merge, or rebase (got {method!r})"})

    cmd = ["pr", "merge", pr_number, f"--{method}", "--auto"]
    if delete_branch:
        cmd.append("--delete-branch")

    result = _run_gh(cmd, cwd=workspace, timeout=60)
    if result["returncode"] != 0:
        return json.dumps({"error": result["stderr"] or "gh pr merge failed"})

    return json.dumps({"success": True, "pr_number": pr_number, "method": method})


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

registry.register(
    name="gh_list_prs",
    handler=_gh_list_prs,
    schema={
        "parameters": {
            "type": "object",
            "properties": {
                "state": {"type": "string", "enum": ["open", "closed", "merged", "all"],
                          "description": "PR state filter (default: open)"},
                "limit": {"type": "integer", "description": "Max PRs to return (default: 20)"},
            },
            "required": [],
        },
    },
    toolset="github",
    description="List pull requests in the current GitHub repository.",
)

registry.register(
    name="gh_create_pr",
    handler=_gh_create_pr,
    schema={
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "PR title"},
                "body": {"type": "string", "description": "PR body / description"},
                "base": {"type": "string", "description": "Base branch (default: main)"},
                "head": {"type": "string", "description": "Head branch (default: current branch)"},
                "draft": {"type": "boolean", "description": "Create as draft PR"},
            },
            "required": ["title"],
        },
    },
    toolset="github",
    description="Open a new pull request in the current GitHub repository.",
)

registry.register(
    name="gh_request_review",
    handler=_gh_request_review,
    schema={
        "parameters": {
            "type": "object",
            "properties": {
                "pr_number": {"type": "integer", "description": "PR number"},
                "reviewers": {"type": "array", "items": {"type": "string"},
                              "description": "GitHub usernames to add as reviewers"},
                "teams": {"type": "array", "items": {"type": "string"},
                          "description": "GitHub team slugs (org/team-slug) to add"},
            },
            "required": ["pr_number"],
        },
    },
    toolset="github",
    description="Add reviewers or teams to an existing pull request.",
)

registry.register(
    name="gh_merge_pr",
    handler=_gh_merge_pr,
    schema={
        "parameters": {
            "type": "object",
            "properties": {
                "pr_number": {"type": "integer", "description": "PR number"},
                "method": {"type": "string", "enum": ["squash", "merge", "rebase"],
                           "description": "Merge method (default: squash)"},
                "delete_branch": {"type": "boolean",
                                  "description": "Delete head branch after merge (default: true)"},
            },
            "required": ["pr_number"],
        },
    },
    toolset="github",
    description="Merge a pull request.",
)
