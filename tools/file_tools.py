"""File system tools for developer agents.

All operations are confined to the current agent's workspace (enforced by
safe_resolve()). Tools self-register into the global registry at import time.

Tools exposed:
  read_file      — read a file (with optional line window)
  write_file     — create or overwrite a file (creates parent dirs)
  patch          — exact-string replacement inside a file
  search_files   — find files by glob pattern or grep for content
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional

from core.registry import registry, tool_error
from tools._context import safe_resolve, get_workspace

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fmt(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False)


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------


def _read_file(path: str, offset: int = 0, limit: Optional[int] = None) -> str:
    """Read a file from the workspace, returning its content.

    Args:
        path:   Path relative to the workspace root.
        offset: First line to return (0-based). Default 0.
        limit:  Maximum number of lines to return. None = all.
    """
    try:
        target = safe_resolve(path)
    except PermissionError as e:
        return tool_error(str(e))

    if not target.exists():
        return tool_error(f"File not found: {path!r}")
    if target.is_dir():
        return tool_error(f"{path!r} is a directory, not a file")

    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return tool_error(f"Cannot read {path!r}: {e}")

    lines = text.splitlines(keepends=True)
    total = len(lines)
    window = lines[offset:] if limit is None else lines[offset : offset + limit]
    content = "".join(window)

    return _fmt(
        {
            "path": path,
            "content": content,
            "total_lines": total,
            "returned_lines": len(window),
            "offset": offset,
        }
    )


registry.register(
    name="read_file",
    handler=_read_file,
    schema={
        "parameters": {
            "type": "object",
            "required": ["path"],
            "properties": {
                "path": {"type": "string", "description": "File path relative to workspace root"},
                "offset": {
                    "type": "integer",
                    "description": "First line to return (0-based)",
                    "default": 0,
                },
                "limit": {"type": "integer", "description": "Max lines to return (omit for all)"},
            },
        }
    },
    toolset="file",
    description="Read a file from the agent workspace. Returns content and line count.",
)


# ---------------------------------------------------------------------------
# write_file
# ---------------------------------------------------------------------------


def _write_file(path: str, content: str) -> str:
    """Write content to a file in the workspace, creating parent directories.

    Args:
        path:    Path relative to the workspace root.
        content: Full file content to write (overwrites existing file).
    """
    try:
        target = safe_resolve(path)
    except PermissionError as e:
        return tool_error(str(e))

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    except OSError as e:
        return tool_error(f"Cannot write {path!r}: {e}")

    logger.info("write_file: wrote %d bytes to %s", len(content.encode()), target)
    return _fmt({"path": path, "bytes_written": len(content.encode("utf-8")), "ok": True})


registry.register(
    name="write_file",
    handler=_write_file,
    schema={
        "parameters": {
            "type": "object",
            "required": ["path", "content"],
            "properties": {
                "path": {"type": "string", "description": "File path relative to workspace root"},
                "content": {"type": "string", "description": "Full file content to write"},
            },
        }
    },
    toolset="file",
    description="Write (create or overwrite) a file in the agent workspace.",
)


# ---------------------------------------------------------------------------
# patch
# ---------------------------------------------------------------------------


def _patch(path: str, old_string: str, new_string: str) -> str:
    """Replace the first occurrence of old_string with new_string in a file.

    Args:
        path:       Path relative to workspace root.
        old_string: Exact text to find (must be unique in the file).
        new_string: Replacement text.
    """
    try:
        target = safe_resolve(path)
    except PermissionError as e:
        return tool_error(str(e))

    if not target.exists():
        return tool_error(f"File not found: {path!r}")

    try:
        original = target.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return tool_error(f"Cannot read {path!r}: {e}")

    count = original.count(old_string)
    if count == 0:
        return tool_error(
            f"old_string not found in {path!r}. " "Provide more context to make the match unique."
        )
    if count > 1:
        return tool_error(
            f"old_string appears {count} times in {path!r}. "
            "Provide more surrounding context to make it unique."
        )

    patched = original.replace(old_string, new_string, 1)
    try:
        target.write_text(patched, encoding="utf-8")
    except OSError as e:
        return tool_error(f"Cannot write {path!r}: {e}")

    return _fmt({"path": path, "ok": True, "replacements": 1})


registry.register(
    name="patch",
    handler=_patch,
    schema={
        "parameters": {
            "type": "object",
            "required": ["path", "old_string", "new_string"],
            "properties": {
                "path": {"type": "string", "description": "File path relative to workspace root"},
                "old_string": {
                    "type": "string",
                    "description": "Exact string to replace (must be unique in file)",
                },
                "new_string": {"type": "string", "description": "Replacement string"},
            },
        }
    },
    toolset="file",
    description="Replace an exact string in a file. old_string must appear exactly once.",
)


# ---------------------------------------------------------------------------
# search_files
# ---------------------------------------------------------------------------


def _search_files(
    pattern: str,
    directory: str = ".",
    grep: Optional[str] = None,
    max_results: int = 50,
) -> str:
    """Search for files by glob pattern, optionally filtering by content.

    Args:
        pattern:     Glob pattern relative to directory (e.g. '**/*.py').
        directory:   Directory to search in, relative to workspace root.
        grep:        If given, only return files containing this string/regex.
        max_results: Maximum number of results to return.
    """
    try:
        base = safe_resolve(directory)
    except PermissionError as e:
        return tool_error(str(e))

    if not base.exists():
        return tool_error(f"Directory not found: {directory!r}")

    matches = []
    workspace = get_workspace().resolve()

    try:
        for candidate in sorted(base.rglob(pattern) if "**" in pattern else base.glob(pattern)):
            if not candidate.is_file():
                continue
            if grep:
                try:
                    text = candidate.read_text(encoding="utf-8", errors="replace")
                    if not re.search(grep, text):
                        continue
                except OSError:
                    continue
            rel = candidate.relative_to(workspace)
            matches.append(str(rel))
            if len(matches) >= max_results:
                break
    except (OSError, re.error) as e:
        return tool_error(f"Search error: {e}")

    return _fmt(
        {
            "pattern": pattern,
            "directory": directory,
            "grep": grep,
            "matches": matches,
            "total": len(matches),
            "truncated": len(matches) >= max_results,
        }
    )


registry.register(
    name="search_files",
    handler=_search_files,
    schema={
        "parameters": {
            "type": "object",
            "required": ["pattern"],
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern, e.g. '**/*.py' or 'src/*.ts'",
                },
                "directory": {
                    "type": "string",
                    "description": "Directory to search in (relative to workspace). Default: '.'",
                    "default": ".",
                },
                "grep": {
                    "type": "string",
                    "description": "Optional regex/string: only return files whose content matches",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum results to return. Default 50",
                    "default": 50,
                },
            },
        }
    },
    toolset="file",
    description="Find files by glob pattern in the workspace. Optionally filter by content.",
)
