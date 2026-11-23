"""Kanban board tools for planning and development agents.

These tools proxy to PipelineDB via the thread-local tool context.
They are used by TechLeadAgent to create task cards and by developer agents
to update task status and post comments.

Registers as: kanban_create, kanban_complete, kanban_block, kanban_comment
All in toolset="kanban".
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from core.registry import registry, tool_error
from tools._context import get_db, get_run_id

logger = logging.getLogger(__name__)


def _fmt(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False)


def _require_db():
    db = get_db()
    if db is None:
        raise RuntimeError(
            "Kanban tools require a PipelineDB context. "
            "Ensure the agent has been initialised with a database."
        )
    return db


# ---------------------------------------------------------------------------
# kanban_create
# ---------------------------------------------------------------------------


def _kanban_create(
    task_id: str,
    title: str,
    assignee: str,
    description: str = "",
    metadata: Optional[dict] = None,
) -> str:
    """Create a new task card on the kanban board.

    Args:
        task_id:     Unique identifier for the task (e.g. 't-001').
        title:       Short task title (imperative phrase).
        assignee:    Assignee role (e.g. 'developer_backend').
        description: Optional longer description.
        metadata:    Optional dict of extra fields (acceptance criteria, etc.).
    """
    try:
        db = _require_db()
        run_id = get_run_id()
        extra = metadata or {}
        if description:
            extra["description"] = description
        db.create_task(
            task_id=task_id,
            run_id=run_id,
            title=title,
            assignee=assignee,
            metadata=extra,
        )
        logger.info("kanban_create: %s (%s) for run %s", task_id, assignee, run_id)
        return _fmt({"task_id": task_id, "status": "ready", "ok": True})
    except Exception as e:
        return tool_error(f"kanban_create failed: {e}")


registry.register(
    name="kanban_create",
    handler=_kanban_create,
    schema={
        "parameters": {
            "type": "object",
            "required": ["task_id", "title", "assignee"],
            "properties": {
                "task_id": {"type": "string", "description": "Unique task ID, e.g. 't-001'"},
                "title": {"type": "string", "description": "Short imperative task title"},
                "assignee": {
                    "type": "string",
                    "description": "Assignee role: developer_backend | developer_ui | developer_devops",
                },
                "description": {"type": "string", "description": "Optional longer description"},
                "metadata": {
                    "type": "object",
                    "description": "Optional extra fields (acceptance_criteria, skills, etc.)",
                },
            },
        }
    },
    toolset="kanban",
    description="Create a new task card on the kanban board for this pipeline run.",
)


# ---------------------------------------------------------------------------
# kanban_complete
# ---------------------------------------------------------------------------


def _kanban_complete(task_id: str, notes: str = "") -> str:
    """Mark a kanban task as done.

    Args:
        task_id: The task to mark done.
        notes:   Optional completion notes.
    """
    try:
        db = _require_db()
        meta = {"completion_notes": notes} if notes else None
        db.update_task_status(task_id, "done", metadata=meta)
        logger.info("kanban_complete: %s", task_id)
        return _fmt({"task_id": task_id, "status": "done", "ok": True})
    except Exception as e:
        return tool_error(f"kanban_complete failed: {e}")


registry.register(
    name="kanban_complete",
    handler=_kanban_complete,
    schema={
        "parameters": {
            "type": "object",
            "required": ["task_id"],
            "properties": {
                "task_id": {"type": "string", "description": "Task ID to mark as done"},
                "notes": {"type": "string", "description": "Optional completion notes"},
            },
        }
    },
    toolset="kanban",
    description="Mark a kanban task as completed.",
)


# ---------------------------------------------------------------------------
# kanban_block
# ---------------------------------------------------------------------------


def _kanban_block(task_id: str, reason: str) -> str:
    """Mark a kanban task as blocked with a reason.

    Args:
        task_id: The task to block.
        reason:  Description of what is blocking progress.
    """
    try:
        db = _require_db()
        db.update_task_status(task_id, "blocked")
        db.add_task_comment(task_id, f"BLOCKED: {reason}", author="agent")
        logger.info("kanban_block: %s — %s", task_id, reason)
        return _fmt({"task_id": task_id, "status": "blocked", "reason": reason, "ok": True})
    except Exception as e:
        return tool_error(f"kanban_block failed: {e}")


registry.register(
    name="kanban_block",
    handler=_kanban_block,
    schema={
        "parameters": {
            "type": "object",
            "required": ["task_id", "reason"],
            "properties": {
                "task_id": {"type": "string", "description": "Task ID to mark as blocked"},
                "reason": {"type": "string", "description": "What is blocking progress"},
            },
        }
    },
    toolset="kanban",
    description="Mark a kanban task as blocked and record the blocking reason.",
)


# ---------------------------------------------------------------------------
# kanban_comment
# ---------------------------------------------------------------------------


def _kanban_comment(task_id: str, comment: str, author: str = "agent") -> str:
    """Post a comment on a kanban task.

    Args:
        task_id: The task to comment on.
        comment: Comment text.
        author:  Author name / role. Default 'agent'.
    """
    try:
        db = _require_db()
        db.add_task_comment(task_id, comment, author=author)
        logger.info("kanban_comment: %s — %s: %.60s", task_id, author, comment)
        return _fmt({"task_id": task_id, "ok": True})
    except Exception as e:
        return tool_error(f"kanban_comment failed: {e}")


registry.register(
    name="kanban_comment",
    handler=_kanban_comment,
    schema={
        "parameters": {
            "type": "object",
            "required": ["task_id", "comment"],
            "properties": {
                "task_id": {"type": "string", "description": "Task ID to comment on"},
                "comment": {"type": "string", "description": "Comment text"},
                "author": {"type": "string", "description": "Author name/role. Default 'agent'"},
            },
        }
    },
    toolset="kanban",
    description="Post a comment on a kanban task card.",
)
