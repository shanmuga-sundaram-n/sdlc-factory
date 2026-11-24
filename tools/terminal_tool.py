"""Terminal (subprocess) tool for developer agents.

Executes commands in the agent's workspace directory. This is a guardrail,
not a sandbox: allowed commands (make, npm run, pip install, git, pytest,
python script.py) still run arbitrary code on the host with the user's
permissions. See SECURITY.md.

What it does:
  - cwd is resolved and verified to be inside the workspace.
  - tools/command_policy.py refuses commands that are not on the allowlist,
    shells and network tools, inline code (python -c, node -e), and absolute,
    home or parent-directory path arguments.
  - The subprocess gets a scrubbed environment (no dashboard token or API keys).
  - Output is capped at MAX_OUTPUT_BYTES to prevent context flooding.
  - A hard timeout (default 60s) kills the subprocess if it hangs.
  - The tool does NOT use shell=True, so there is no shell expansion or chaining.

Registers as: terminal  (toolset="terminal")
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
from typing import Optional

from core.registry import registry, tool_error
from tools import command_policy
from tools._context import agent_env, get_workspace, safe_resolve

logger = logging.getLogger(__name__)

MAX_OUTPUT_BYTES = 32_768  # 32 KB cap on stdout + stderr combined


def _terminal(command: str, cwd: Optional[str] = None, timeout: int = 60) -> str:
    """Execute a shell command in the workspace and return output.

    Args:
        command: Shell command string (will be split with shlex — no shell=True).
        cwd:     Working directory relative to workspace root. Default: workspace root.
        timeout: Seconds before the process is killed. Default 60.
    """
    workspace = get_workspace()

    if cwd:
        try:
            run_dir = safe_resolve(cwd)
        except PermissionError as e:
            return tool_error(str(e))
        if not run_dir.exists():
            return tool_error(f"cwd not found: {cwd!r}")
    else:
        run_dir = workspace

    try:
        args = shlex.split(command)
    except ValueError as e:
        return tool_error(f"Cannot parse command {command!r}: {e}")

    if not args:
        return tool_error("Empty command")

    cwd_rel = os.path.relpath(run_dir, workspace.resolve())
    policy_error = command_policy.check(args, cwd_rel)
    if policy_error:
        logger.warning("terminal refused: %s (%s)", command, policy_error)
        return tool_error(policy_error)

    logger.info("terminal: %s (cwd=%s timeout=%ds)", command, run_dir, timeout)

    try:
        result = subprocess.run(
            args,
            cwd=str(run_dir),
            env=agent_env(),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return tool_error(f"Command timed out after {timeout}s: {command!r}")
    except FileNotFoundError:
        return tool_error(f"Command not found: {args[0]!r}")
    except OSError as e:
        return tool_error(f"OS error running {command!r}: {e}")

    stdout = result.stdout
    stderr = result.stderr

    # Cap total output to prevent context overflow
    total = len(stdout) + len(stderr)
    if total > MAX_OUTPUT_BYTES:
        half = MAX_OUTPUT_BYTES // 2
        stdout = stdout[:half] + ("\n...[truncated]" if len(stdout) > half else "")
        stderr = stderr[:half] + ("\n...[truncated]" if len(stderr) > half else "")

    return json.dumps(
        {
            "command": command,
            "cwd": str(run_dir.relative_to(workspace)) if run_dir != workspace else ".",
            "returncode": result.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "ok": result.returncode == 0,
        },
        ensure_ascii=False,
    )


registry.register(
    name="terminal",
    handler=_terminal,
    schema={
        "parameters": {
            "type": "object",
            "required": ["command"],
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to execute (e.g. 'pytest tests/ --tb=short')",
                },
                "cwd": {
                    "type": "string",
                    "description": "Working directory relative to workspace root. Default: workspace root.",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Seconds before the process is killed. Default 60.",
                    "default": 60,
                },
            },
        }
    },
    toolset="terminal",
    description=(
        "Execute a shell command in the agent workspace. "
        "Returns stdout, stderr, and exit code. Timeout default 60s."
    ),
)
