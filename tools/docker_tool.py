"""Docker tools for the devops toolset.

Provides docker_build and docker_run as structured tools so DeveloperDevOpsAgent
can build and verify Docker images without parsing raw terminal output.

Safety: all paths are resolved through safe_resolve() so agents cannot
reference files outside the run workspace.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.registry import registry
from tools._context import get_workspace, safe_resolve

_MAX_OUTPUT_BYTES = 16_384


def _run_docker(args: List[str], cwd: Path, timeout: int) -> Dict[str, Any]:
    """Run a docker sub-command; return {returncode, stdout, stderr}."""
    try:
        proc = subprocess.run(
            ["docker"] + args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "returncode": proc.returncode,
            "stdout": proc.stdout[-_MAX_OUTPUT_BYTES:],
            "stderr": proc.stderr[-_MAX_OUTPUT_BYTES:],
        }
    except subprocess.TimeoutExpired:
        return {"returncode": -1, "stdout": "", "stderr": f"docker timed out after {timeout}s"}
    except FileNotFoundError:
        return {"returncode": -1, "stdout": "", "stderr": "docker binary not found in PATH"}


def docker_build(
    tag: str,
    dockerfile: str = "Dockerfile",
    context_dir: str = ".",
    build_args: Optional[Dict[str, str]] = None,
    timeout: int = 300,
) -> str:
    """Build a Docker image from the workspace.

    Parameters
    ----------
    tag:
        Image name and optional tag, e.g. ``myapp:1.0.0``.
    dockerfile:
        Path to the Dockerfile relative to the workspace.
    context_dir:
        Build context directory relative to the workspace.
    build_args:
        Optional ``--build-arg KEY=VALUE`` pairs.
    timeout:
        Maximum seconds before the build is killed.

    Returns
    -------
    JSON with ``success``, ``image_id``, ``tag``, ``stdout``, and ``stderr``.
    """
    workspace = get_workspace()
    context = safe_resolve(context_dir) if context_dir not in (".", "") else workspace
    dockerfile_path = (
        safe_resolve(dockerfile) if dockerfile != "Dockerfile" else workspace / "Dockerfile"
    )

    cmd = ["build", "-t", tag, "-f", str(dockerfile_path)]
    if build_args:
        for k, v in build_args.items():
            cmd += ["--build-arg", f"{k}={v}"]
    cmd.append(str(context))

    result = _run_docker(cmd, cwd=workspace, timeout=timeout)
    success = result["returncode"] == 0

    # Extract image ID from build output
    image_id = ""
    if success:
        match = re.search(r"Successfully built ([0-9a-f]{12})", result["stdout"])
        if match:
            image_id = match.group(1)
        # BuildKit output format
        match2 = re.search(r"writing image sha256:([0-9a-f]{64})", result["stdout"])
        if match2:
            image_id = "sha256:" + match2.group(1)[:12]

    return json.dumps(
        {
            "success": success,
            "tag": tag,
            "image_id": image_id,
            "return_code": result["returncode"],
            "stdout": result["stdout"],
            "stderr": result["stderr"],
        },
        ensure_ascii=False,
    )


def docker_run(
    tag: str,
    command: str = "",
    env: Optional[Dict[str, str]] = None,
    timeout: int = 60,
) -> str:
    """Run a Docker container and capture its output.

    The container is always removed after it exits (``--rm``).
    Suitable for smoke tests and one-shot commands.

    Parameters
    ----------
    tag:
        Image tag to run.
    command:
        Command to pass to the container entry point.
    env:
        Optional environment variables as a ``{KEY: VALUE}`` dict.
    timeout:
        Maximum seconds before the container is killed.

    Returns
    -------
    JSON with ``success``, ``exit_code``, ``stdout``, and ``stderr``.
    """
    workspace = get_workspace()
    cmd = ["run", "--rm"]
    if env:
        for k, v in env.items():
            cmd += ["-e", f"{k}={v}"]
    cmd.append(tag)
    if command:
        cmd += command.split()

    result = _run_docker(cmd, cwd=workspace, timeout=timeout)
    return json.dumps(
        {
            "success": result["returncode"] == 0,
            "exit_code": result["returncode"],
            "stdout": result["stdout"],
            "stderr": result["stderr"],
        },
        ensure_ascii=False,
    )


def docker_image_exists(tag: str) -> str:
    """Check whether a Docker image tag exists locally.

    Returns JSON ``{exists: bool, tag: str}``.
    """
    workspace = get_workspace()
    result = _run_docker(["image", "inspect", tag], cwd=workspace, timeout=15)
    return json.dumps(
        {
            "exists": result["returncode"] == 0,
            "tag": tag,
        }
    )


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

registry.register(
    name="docker_build",
    handler=docker_build,
    schema={
        "name": "docker_build",
        "description": (
            "Build a Docker image from the workspace. "
            "Returns success flag, image ID, and build output."
        ),
        "parameters": {
            "type": "object",
            "required": ["tag"],
            "properties": {
                "tag": {
                    "type": "string",
                    "description": "Image name and tag, e.g. 'myapp:1.0.0'.",
                },
                "dockerfile": {
                    "type": "string",
                    "description": "Path to Dockerfile relative to workspace (default: Dockerfile).",
                    "default": "Dockerfile",
                },
                "context_dir": {
                    "type": "string",
                    "description": "Build context directory relative to workspace (default: workspace root).",
                    "default": ".",
                },
                "build_args": {
                    "type": "object",
                    "description": "Optional --build-arg KEY=VALUE pairs.",
                    "additionalProperties": {"type": "string"},
                },
                "timeout": {
                    "type": "integer",
                    "description": "Maximum seconds for the build (default: 300).",
                    "default": 300,
                },
            },
        },
    },
    toolset="devops",
)

registry.register(
    name="docker_run",
    handler=docker_run,
    schema={
        "name": "docker_run",
        "description": (
            "Run a Docker container and capture its output (container is removed after exit). "
            "Use for smoke tests and one-shot verification."
        ),
        "parameters": {
            "type": "object",
            "required": ["tag"],
            "properties": {
                "tag": {
                    "type": "string",
                    "description": "Docker image tag to run.",
                },
                "command": {
                    "type": "string",
                    "description": "Command string to pass to the container entry point.",
                    "default": "",
                },
                "env": {
                    "type": "object",
                    "description": "Environment variables as {KEY: VALUE}.",
                    "additionalProperties": {"type": "string"},
                },
                "timeout": {
                    "type": "integer",
                    "description": "Maximum seconds before the container is killed (default: 60).",
                    "default": 60,
                },
            },
        },
    },
    toolset="devops",
)

registry.register(
    name="docker_image_exists",
    handler=docker_image_exists,
    schema={
        "name": "docker_image_exists",
        "description": "Check whether a Docker image tag exists locally. Returns {exists: bool}.",
        "parameters": {
            "type": "object",
            "required": ["tag"],
            "properties": {
                "tag": {"type": "string", "description": "Image tag to inspect."},
            },
        },
    },
    toolset="devops",
)
