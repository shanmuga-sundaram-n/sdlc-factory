"""QA fix-loop orchestrator.

Runs the QAEngineerAgent, inspects the result, and if the quality gate fails
dispatches developer fix tasks (up to MAX_FIX_ITERATIONS). After each fix
the QA agent re-runs against the updated workspace. If all iterations are
exhausted without a pass the artifact is flagged for HITL escalation.

Design notes:
- QALoop is intentionally decoupled from SDLCPipeline to avoid circular
  imports; it receives only the objects it needs.
- build_agent_config_fn is a bound method on SDLCPipeline; type-annotated
  as Callable to keep this module free of pipeline imports.
- Fix tasks inherit the developer role recorded on the original kanban task
  (resolved via _resolve_fix_role); falls back to developer_backend.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


def _unwrap(artifact: Any) -> Any:
    """Unwrap single-role stage artifact: {role: payload} → payload."""
    if isinstance(artifact, dict) and len(artifact) == 1:
        only_val = next(iter(artifact.values()))
        if isinstance(only_val, dict):
            return only_val
    return artifact


def _rewrap(original: Any, inner: Dict) -> Any:
    """Rewrap inner dict into its original role key if applicable."""
    if isinstance(original, dict) and len(original) == 1:
        role_key = next(iter(original))
        if isinstance(original[role_key], dict):
            return {role_key: inner}
    return inner


class QALoop:
    """QA agent → developer fix → QA retry cycle.

    Parameters
    ----------
    agent_registry:
        Maps role string → agent class (same dict as SDLCPipeline._agent_registry).
    config:
        The pipeline config dict (same as SDLCPipeline.config).
    db:
        Shared PipelineDB instance.
    build_agent_config_fn:
        Bound method SDLCPipeline._build_agent_config(role, stage_def, pr).
    max_fix_iterations:
        Maximum developer fix passes before escalating to HITL.
    """

    MAX_FIX_ITERATIONS = 3

    def __init__(
        self,
        agent_registry: Dict[str, type],
        config: Dict[str, Any],
        db: Any,
        build_agent_config_fn: Callable,
        max_fix_iterations: int = MAX_FIX_ITERATIONS,
    ):
        self._registry = agent_registry
        self._config = config
        self._db = db
        self._build_config = build_agent_config_fn
        self._max_iterations = max_fix_iterations

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(
        self,
        pr: Any,
        stage_def: Any,
        initial_artifact: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Run the fix loop and return the final QA artifact.

        If the gate passes immediately, returns *initial_artifact* unchanged.
        After each fix iteration the QA agent re-runs. If all iterations are
        exhausted without passing, the returned artifact will contain
        ``escalated_to_hitl: true``.
        """
        stage_artifact = initial_artifact
        inner = _unwrap(stage_artifact)

        if inner.get("gate_passed", False):
            logger.info("QA loop: gate passed on first attempt")
            return stage_artifact

        critical_bugs = [b for b in inner.get("bugs", []) if b.get("severity") == "critical"]
        if not critical_bugs:
            logger.info(
                "QA loop: gate_passed=false but no critical bugs — "
                "leaving artifact for HITL review"
            )
            return stage_artifact

        logger.info(
            "QA loop: gate failed with %d critical bug(s); starting fix iterations",
            len(critical_bugs),
        )

        for attempt in range(1, self._max_iterations + 1):
            logger.info("QA fix iteration %d/%d", attempt, self._max_iterations)

            dispatched = self._run_fix_iteration(pr, stage_def, inner, attempt)
            if not dispatched:
                logger.warning("Fix iteration %d: no fix agents ran; stopping", attempt)
                break

            # Re-run QA against the (potentially) updated workspace
            stage_artifact = self._rerun_qa(pr, stage_def)
            if not stage_artifact:
                logger.warning("QA re-run produced no artifact on iteration %d", attempt)
                break

            inner = _unwrap(stage_artifact)
            if inner.get("gate_passed", False):
                logger.info("QA loop: gate passed after fix iteration %d", attempt)
                return stage_artifact

            remaining = [b for b in inner.get("bugs", []) if b.get("severity") == "critical"]
            logger.info(
                "Iteration %d complete: still %d critical bug(s) remaining",
                attempt, len(remaining),
            )

        # All iterations exhausted — flag for HITL escalation
        inner = dict(inner)
        inner["escalated_to_hitl"] = True
        inner["escalation_reason"] = (
            f"QA fix loop exhausted after {self._max_iterations} attempt(s); "
            "manual intervention required"
        )
        final = _rewrap(initial_artifact, inner)
        logger.warning(
            "QA loop exhausted after %d iteration(s); escalating to HITL",
            self._max_iterations,
        )
        return final

    # ------------------------------------------------------------------
    # Fix iteration
    # ------------------------------------------------------------------

    def _run_fix_iteration(
        self,
        pr: Any,
        stage_def: Any,
        qa_inner: Dict,
        iteration: int,
    ) -> bool:
        """Create and dispatch developer fix tasks. Returns True if any ran."""
        bugs = [
            b for b in qa_inner.get("bugs", [])
            if b.get("severity") in ("critical", "major")
        ]
        if not bugs:
            return False

        # Group by related_task so each original task gets at most one fix agent
        by_task: Dict[str, List[Dict]] = {}
        for bug in bugs:
            key = bug.get("related_task") or "_unknown"
            by_task.setdefault(key, []).append(bug)

        any_dispatched = False
        for original_task_id, task_bugs in by_task.items():
            role = self._resolve_fix_role(original_task_id)
            fix_task_id = f"fix-i{iteration}-{original_task_id[:20]}-{uuid.uuid4().hex[:4]}"

            self._db.create_task(
                task_id=fix_task_id,
                run_id=pr.run_id,
                title=f"[Fix i{iteration}] {len(task_bugs)} bug(s) from {original_task_id}",
                assignee=role,
                metadata={"iteration": iteration, "bugs": task_bugs,
                          "original_task_id": original_task_id},
            )

            description = self._build_fix_description(pr, task_bugs, iteration)
            success = self._dispatch_fix_agent(pr, stage_def, fix_task_id, role, description)

            if success:
                self._db.update_task_status(fix_task_id, "done")
                any_dispatched = True
            else:
                self._db.update_task_status(fix_task_id, "blocked")
                self._db.add_task_comment(
                    fix_task_id,
                    f"Fix agent failed on iteration {iteration}",
                    author="qa_loop",
                )

        return any_dispatched

    def _resolve_fix_role(self, original_task_id: str) -> str:
        """Return the developer role that owns the original task, or developer_backend."""
        if original_task_id and original_task_id != "_unknown":
            task = self._db.get_task(original_task_id)
            if task:
                return task.get("assignee", "developer_backend")
        return "developer_backend"

    def _build_fix_description(
        self, pr: Any, bugs: List[Dict], iteration: int
    ) -> str:
        lines = [
            f"## Fix Task — Iteration {iteration}",
            "",
            "The QA gate failed. The following bugs were filed against your code.",
            "Read the relevant source files, identify the root cause, and fix it.",
            "Do not suppress the tests — fix the underlying defect.",
            "",
            "## Bug Reports",
        ]
        for bug in bugs:
            lines += [
                f"### {bug.get('id', 'BUG-?')}: {bug.get('title', 'Unknown')}",
                f"**Severity**: {bug.get('severity', 'unknown')}",
                f"**Related task**: {bug.get('related_task', 'n/a')}",
                "**Steps to reproduce**:",
                "```",
                bug.get("steps_to_reproduce", "See bug title"),
                "```",
                "",
            ]

        failing_tests = pr.artifact_bundle.get_stage("quality_gate") or {}
        inner = _unwrap(failing_tests)
        test_list = inner.get("failing_tests", [])
        if test_list:
            lines += ["## Failing Tests", ""]
            for t in test_list[:20]:
                lines.append(f"- `{t.get('nodeid', t.get('test_id', '?'))}`: {t.get('message', '')[:120]}")
            lines.append("")

        lines += [
            "## Instructions",
            "1. Use `read_file` to inspect the relevant source files.",
            "2. Fix the root cause.",
            "3. Optionally use `run_pytest` to verify the fix before completing.",
            "4. Emit your artifact with `files_written` and `tests_passing: true`.",
        ]
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Agent dispatch helpers
    # ------------------------------------------------------------------

    def _dispatch_fix_agent(
        self,
        pr: Any,
        stage_def: Any,
        task_id: str,
        role: str,
        description: str,
    ) -> bool:
        agent_cls = self._registry.get(role)
        if agent_cls is None:
            logger.error("QA loop: no agent class for fix role %r", role)
            return False

        agent_config = self._build_config(role, stage_def, pr)
        agent = agent_cls(
            config=agent_config,
            project_memory=pr.project_memory,
            artifact_bundle=pr.artifact_bundle,
            db=self._db,
        )

        result = agent.run(
            task_description=description,
            stage=stage_def.name,
            task_id=task_id,
        )

        if result.success:
            logger.info("Fix agent %s completed task %s", role, task_id)
            return True

        logger.warning(
            "Fix agent %s failed for task %s: %s", role, task_id, result.error
        )
        return False

    def _rerun_qa(self, pr: Any, stage_def: Any) -> Optional[Dict[str, Any]]:
        """Instantiate and run QAEngineerAgent; return the new stage artifact."""
        agent_cls = self._registry.get("qa_engineer")
        if agent_cls is None:
            logger.error("QA loop: no qa_engineer class in registry")
            return None

        agent_config = self._build_config("qa_engineer", stage_def, pr)
        agent = agent_cls(
            config=agent_config,
            project_memory=pr.project_memory,
            artifact_bundle=pr.artifact_bundle,
            db=self._db,
        )

        result = agent.run(
            task_description=(
                "Re-run the full QA test suite after the latest fix iteration. "
                "Produce a fresh test report: update all counts, coverage, bug status, "
                "and set gate_passed accordingly."
            ),
            stage=stage_def.name,
            task_id=f"qa-rerun-{uuid.uuid4().hex[:6]}",
        )

        if result.success and result.artifact:
            return {"qa_engineer": result.artifact}

        logger.warning("QA re-run failed: %s", result.error)
        return None
