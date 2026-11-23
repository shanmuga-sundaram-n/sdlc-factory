"""Thread-local context for tool handlers.

Tool modules (file_tools, terminal_tool, kanban_tools) are stateless singletons
registered in the global registry. They need access to the per-agent runtime
state (workspace path, DB handle, run_id) without those values being passed as
explicit tool arguments (which would expose internals to the model).

Pattern: BaseSDLCAgent calls set_tool_context() before each _dispatch_tools()
batch. Tool handlers read the values via get_workspace() / get_db() / get_run_id().
Thread-local storage keeps parallel developer agents safely isolated.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    pass

_local = threading.local()


def set_tool_context(
    workspace: Path,
    db: Optional[Any] = None,
    run_id: str = "",
) -> None:
    """Called by BaseSDLCAgent before each tool dispatch batch."""
    _local.workspace = workspace
    _local.db = db
    _local.run_id = run_id


def get_workspace() -> Path:
    return getattr(_local, "workspace", Path("."))


def get_db() -> Optional[Any]:
    return getattr(_local, "db", None)


def get_run_id() -> str:
    return getattr(_local, "run_id", "")


def safe_resolve(path_str: str) -> Path:
    """Resolve path_str relative to the current workspace and verify it stays inside.

    Raises PermissionError if the resolved path escapes the workspace root.
    This prevents directory traversal attacks (../../etc/passwd etc.).
    """
    workspace = get_workspace().resolve()
    candidate = (workspace / path_str).resolve()
    try:
        candidate.relative_to(workspace)
    except ValueError:
        raise PermissionError(
            f"Path {path_str!r} resolves to {candidate} which is outside "
            f"the workspace {workspace}"
        )
    return candidate


# Variables an agent-run subprocess must not inherit: the dashboard token and the
# provider/service secrets. Agent-written code runs through the terminal and pytest
# tools, so anything left in the environment is readable by it.
SECRET_ENV_VARS = (
    "SDLC_UI_TOKEN",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "SLACK_BOT_TOKEN",
    "HITL_WEBHOOK_URL",
    "BRAVE_SEARCH_API_KEY",
    "GH_TOKEN",
    "GITHUB_TOKEN",
)


def agent_env() -> dict:
    """Environment for subprocesses that run agent-controlled code."""
    return {k: v for k, v in os.environ.items() if k not in SECRET_ENV_VARS}
