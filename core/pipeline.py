"""Pipeline Orchestrator — the SDLC stage machine.

Manages the full run: idea → delivery via two execution modes:
  1. Sequential stage machine — artifact routing, gate evaluation, checkpoint/resume
  2. Parallel developer dispatch — ThreadPoolExecutor fan-out across developer agents

Usage:
    pipeline = SDLCPipeline.from_config("sdlc_config.yaml")
    result = pipeline.run("Build a FastAPI todo app with user authentication")
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.memory import ArtifactBundle, ProjectMemory, scan_for_injection
from core.session import PipelineDB, get_default_db
from core.telemetry import pipeline_span, stage_span
from gateway.hitl import Decision, HITLGateway

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Stage definitions
# ---------------------------------------------------------------------------


@dataclass
class StageDefinition:
    name: str
    agent_roles: List[str]  # roles that run in this stage
    parallel: bool = False  # True → parallel fan-out of agent_roles
    hitl_gate: bool = False  # True → pause for human approval
    max_agent_timeout: float = 600.0  # seconds per agent
    depends_on: List[str] = field(default_factory=list)  # prior stage names


# The canonical SDLC pipeline stage order
SDLC_STAGES: List[StageDefinition] = [
    StageDefinition("discovery", ["domain_expert", "product_owner"]),
    StageDefinition("requirements", ["business_analyst"], hitl_gate=True),
    StageDefinition("architecture", ["architect"], hitl_gate=True, max_agent_timeout=900.0),
    StageDefinition("planning", ["tech_lead"]),
    StageDefinition(
        "development",
        ["developer_backend", "developer_ui", "developer_devops"],
        parallel=True,
        max_agent_timeout=900.0,
    ),
    StageDefinition("quality_gate", ["qa_engineer"], hitl_gate=True),
    StageDefinition("delivery", ["release_manager"], hitl_gate=True),
]


# ---------------------------------------------------------------------------
# PipelineRun state
# ---------------------------------------------------------------------------


@dataclass
class PipelineRun:
    run_id: str
    idea: str
    config: Dict[str, Any]
    artifact_bundle: ArtifactBundle
    project_memory: ProjectMemory
    db: PipelineDB
    workspace: Path


# ---------------------------------------------------------------------------
# SDLCPipeline
# ---------------------------------------------------------------------------


class SDLCPipeline:
    """The SDLC stage machine.

    Instantiate with from_config() or directly.  Call run(idea) to start.
    """

    def __init__(
        self,
        config: Dict[str, Any],
        stages: Optional[List[StageDefinition]] = None,
        db: Optional[PipelineDB] = None,
        hitl_gateway: Optional[HITLGateway] = None,
    ):
        self.config = config
        self.stages = stages or SDLC_STAGES
        self.db = db or get_default_db()
        self._workspace = Path(config.get("workspace", "./output")).expanduser()
        self._workspace.mkdir(parents=True, exist_ok=True)
        self._default_gate_mode = config.get("governance", {}).get("default_mode", "cli")
        self._hitl = hitl_gateway or HITLGateway()
        self._agent_registry: Dict[str, type] = {}
        # Single-worker executor for non-blocking artifact disk writes.
        # max_workers=1 serialises writes so files are never partially overlapping.
        self._disk_writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="artifact-writer")
        self._register_builtin_agents()
        self._discover_tools()

    # ------------------------------------------------------------------
    # Agent registry + tool discovery
    # ------------------------------------------------------------------

    def _discover_tools(self) -> None:
        """Import tool modules so they self-register into the global registry."""
        try:
            import tools  # noqa: F401 — side-effect: all tools register
        except Exception as exc:
            logger.warning("Tool discovery failed: %s", exc)

    def _register_builtin_agents(self) -> None:
        """Import and register all built-in + custom agent role classes."""
        from agents.roles import AGENT_ROLE_MAP

        self._agent_registry.update(AGENT_ROLE_MAP)
        # Load custom YAML-defined roles (custom_roles/*.yaml); never raises
        try:
            from agents.custom_loader import load_custom_roles

            custom = load_custom_roles()
            self._agent_registry.update(custom)
        except Exception as exc:
            logger.warning("Custom role loading failed: %s", exc)

    def register_agent(self, role: str, agent_cls: type) -> None:
        self._agent_registry[role] = agent_cls

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self, idea: str, *, resume_run_id: Optional[str] = None) -> Dict[str, Any]:
        """Run the full SDLC pipeline for the given idea.

        If resume_run_id is given, load existing state and resume from the
        last approved stage (checkpoint-resume).
        """
        if resume_run_id:
            return self._resume(resume_run_id)

        scan_for_injection(idea)

        run_id = f"run-{uuid.uuid4().hex[:8]}"
        self.db.create_run(run_id, idea, self.config)

        project_memory = ProjectMemory(self._workspace / run_id)
        artifact_bundle = ArtifactBundle(run_id)

        pr = PipelineRun(
            run_id=run_id,
            idea=idea,
            config=self.config,
            artifact_bundle=artifact_bundle,
            project_memory=project_memory,
            db=self.db,
            workspace=self._workspace / run_id,
        )
        pr.workspace.mkdir(parents=True, exist_ok=True)
        self._git_init(pr.workspace, idea)

        logger.info("Pipeline run %s starting | idea=%s…", run_id, idea[:60])
        with pipeline_span(run_id, idea):
            return self._execute(pr)

    def _resume(self, run_id: str) -> Dict[str, Any]:
        run_row = self.db.get_run(run_id)
        if not run_row:
            raise ValueError(f"Run {run_id} not found")

        idea = run_row["idea"]
        last_stage = self.db.get_last_completed_stage(run_id)
        logger.info("Resuming run %s from last approved stage: %s", run_id, last_stage)

        project_memory = ProjectMemory(self._workspace / run_id)

        # Rebuild artifact bundle from DB
        artifact_bundle = ArtifactBundle(run_id)
        for stage_def in self.stages:
            artifact = self.db.get_artifact(run_id, stage_def.name)
            if artifact:
                artifact_bundle.set_stage(stage_def.name, artifact)

        pr = PipelineRun(
            run_id=run_id,
            idea=idea,
            config=self.config,
            artifact_bundle=artifact_bundle,
            project_memory=project_memory,
            db=self.db,
            workspace=self._workspace / run_id,
        )

        # Collect stages that have already been approved so _execute can skip them
        completed_names = set()
        for stage_def in self.stages:
            if self.db.get_artifact(run_id, stage_def.name):
                row = (
                    self.db._conn()
                    .execute(
                        "SELECT status FROM stage_artifacts WHERE run_id=? AND stage=?",
                        (run_id, stage_def.name),
                    )
                    .fetchone()
                )
                if row and row["status"] == "approved":
                    completed_names.add(stage_def.name)

        return self._execute(pr, skip_stages=completed_names)

    # ------------------------------------------------------------------
    # Internal: stage execution
    # ------------------------------------------------------------------

    def _execute(
        self,
        pr: PipelineRun,
        skip_stages: Optional[set] = None,
    ) -> Dict[str, Any]:
        skip = skip_stages or set()
        final_artifact = {}

        for stage_def in self.stages:
            if stage_def.name in skip:
                logger.info("Stage %s: skipping (already completed)", stage_def.name)
                continue

            self.db.update_run_status(pr.run_id, "running", stage=stage_def.name)
            logger.info("Stage %s: starting", stage_def.name)

            with stage_span(pr.run_id, stage_def.name, parallel=stage_def.parallel) as span:
                if stage_def.parallel:
                    stage_artifact = self._run_parallel_stage(pr, stage_def)
                else:
                    stage_artifact = self._run_sequential_stage(pr, stage_def)

                if stage_artifact is None:
                    logger.error("Stage %s produced no artifact; halting pipeline", stage_def.name)
                    self.db.update_run_status(pr.run_id, "failed", stage=stage_def.name)
                    span.set_attribute("outcome", "failed")
                    return {"run_id": pr.run_id, "status": "failed", "failed_stage": stage_def.name}

                # QA fix loop: attempt developer fixes before the HITL gate
                if stage_def.name == "quality_gate":
                    stage_artifact = self._run_qa_loop(pr, stage_def, stage_artifact)

                # Persist artifact to DB and disk
                self.db.save_artifact(pr.run_id, stage_def.name, stage_artifact)
                pr.artifact_bundle.set_stage(stage_def.name, stage_artifact)
                self._write_artifact_to_disk(pr.workspace, stage_def.name, stage_artifact)

                # HITL gate
                if stage_def.hitl_gate:
                    gate_cfg = self._get_gate_config(stage_def.name)
                    gate_mode = gate_cfg.get("mode", self._default_gate_mode)
                    decision, rationale, stage_artifact = self._hitl.request_approval(
                        stage=stage_def.name,
                        artifact=stage_artifact,
                        run_id=pr.run_id,
                        mode=gate_mode,
                        config=gate_cfg,
                    )
                    actor = "auto" if gate_mode == "auto" else "operator"
                    self.db.log_decision(
                        pr.run_id, stage_def.name, decision.value, actor, rationale
                    )
                    logger.info("Stage %s gate: %s — %s", stage_def.name, decision.value, rationale)
                    span.set_attribute("gate.decision", decision.value)
                    span.set_attribute("gate.mode", gate_mode)

                    if decision == Decision.REJECTED:
                        self.db.update_run_status(pr.run_id, "paused", stage=stage_def.name)
                        span.set_attribute("outcome", "paused")
                        return {
                            "run_id": pr.run_id,
                            "status": "paused",
                            "paused_at": stage_def.name,
                            "rationale": rationale,
                        }

                    if decision == Decision.AMENDED:
                        self.db.save_artifact(pr.run_id, stage_def.name, stage_artifact)
                        pr.artifact_bundle.set_stage(stage_def.name, stage_artifact)
                        self._write_artifact_to_disk(pr.workspace, stage_def.name, stage_artifact)
                        stage_order = [s.name for s in self.stages]
                        cleared = self.db.clear_stages_after(pr.run_id, stage_def.name, stage_order)
                        if cleared:
                            logger.info(
                                "AMENDED: cleared %d downstream stages: %s", len(cleared), cleared
                            )

                self.db.approve_artifact(pr.run_id, stage_def.name, approved_by="pipeline")
                self._git_commit_stage(pr.workspace, stage_def.name)
                span.set_attribute("outcome", "completed")
                final_artifact = stage_artifact
                logger.info("Stage %s: completed", stage_def.name)

        self.db.update_run_status(pr.run_id, "completed")
        logger.info("Pipeline run %s completed successfully", pr.run_id)
        return {
            "run_id": pr.run_id,
            "status": "completed",
            "final_artifact": final_artifact,
            "audit_log": self.db.get_audit_log(pr.run_id),
        }

    def _run_sequential_stage(
        self,
        pr: PipelineRun,
        stage_def: StageDefinition,
    ) -> Optional[Dict[str, Any]]:
        """Run each role in the stage sequentially, chaining outputs."""
        combined: Dict[str, Any] = {}

        for role in stage_def.agent_roles:
            agent_cls = self._agent_registry.get(role)
            if agent_cls is None:
                logger.error("No agent class registered for role %r", role)
                return None

            agent_config = self._build_agent_config(role, stage_def, pr)
            agent = agent_cls(
                config=agent_config,
                project_memory=pr.project_memory,
                artifact_bundle=pr.artifact_bundle,
                db=pr.db,
            )

            task_description = self._build_task_description(role, stage_def, pr)
            result = agent.run(
                task_description=task_description,
                stage=stage_def.name,
                task_id=f"{stage_def.name}-{role}",
            )

            self._record_agent_tokens(pr.run_id, stage_def.name, role, result, agent_config.model)

            if not result.success:
                logger.error("Role %s in stage %s failed: %s", role, stage_def.name, result.error)
                return None

            if result.artifact:
                combined[role] = result.artifact

        return combined if combined else None

    def _run_parallel_stage(
        self,
        pr: PipelineRun,
        stage_def: StageDefinition,
    ) -> Optional[Dict[str, Any]]:
        """Fan-out all roles in the stage to a thread pool.

        Each developer agent:
        - Gets its own task_id and workspace subdirectory
        - Runs in a worker thread with a non-interactive approval callback
        - Results are collected into a combined artifact
        """
        max_workers = self.config.get("parallel", {}).get("max_concurrent_developers", 4)
        timeout = stage_def.max_agent_timeout

        tasks = self._build_development_tasks(stage_def, pr)

        # Approval callback for subagent threads
        def _subagent_approval(command: str, description: str, **kwargs) -> str:
            auto_approve = self.config.get("parallel", {}).get("subagent_auto_approve", False)
            if auto_approve:
                logger.warning("Subagent auto-approved: %s", command)
                return "once"
            logger.warning(
                "Subagent auto-denied: %s (set subagent_auto_approve: true to allow)", command
            )
            return "deny"

        def _run_task(task_spec: Dict[str, Any]) -> Tuple[str, Optional[Dict]]:
            role = task_spec["role"]
            task_id = task_spec["task_id"]
            try:
                agent_cls = self._agent_registry.get(role)
                if agent_cls is None:
                    return task_id, None

                agent_config = self._build_agent_config(role, stage_def, pr)
                agent_config.workspace = pr.workspace / "tasks" / task_id
                agent_config.workspace.mkdir(parents=True, exist_ok=True)

                agent = agent_cls(
                    config=agent_config,
                    project_memory=pr.project_memory,
                    artifact_bundle=pr.artifact_bundle,
                    db=pr.db,
                )

                self.db.update_task_status(task_id, "in_progress")
                result = agent.run(
                    task_description=task_spec["description"],
                    stage=stage_def.name,
                    task_id=task_id,
                )

                self._record_agent_tokens(
                    pr.run_id, stage_def.name, role, result, agent_config.model
                )

                if result.success:
                    self.db.update_task_status(task_id, "done", metadata=result.artifact)
                    return task_id, result.artifact
                else:
                    self.db.update_task_status(task_id, "blocked")
                    self.db.add_task_comment(task_id, f"Failed: {result.error}", author=role)
                    return task_id, None

            except Exception as exc:
                logger.exception("Task %s (role=%s) raised: %s", task_id, role, exc)
                self.db.update_task_status(task_id, "blocked")
                return task_id, None

        combined: Dict[str, Any] = {}
        failed: List[str] = []

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            future_to_task = {pool.submit(_run_task, t): t for t in tasks}
            for future in as_completed(future_to_task, timeout=timeout + 30):
                task_spec = future_to_task[future]
                try:
                    task_id, artifact = future.result(timeout=5)
                    if artifact is not None:
                        combined[task_id] = artifact
                    else:
                        failed.append(task_spec["task_id"])
                except FuturesTimeoutError:
                    failed.append(task_spec["task_id"])
                    logger.error("Task %s timed out", task_spec["task_id"])
                except Exception as exc:
                    failed.append(task_spec["task_id"])
                    logger.error("Task %s raised: %s", task_spec["task_id"], exc)

        if failed:
            logger.warning(
                "Parallel stage %s: %d tasks failed: %s", stage_def.name, len(failed), failed
            )
            if len(failed) == len(tasks):
                return None

        combined["_failed_tasks"] = failed
        return combined

    # ------------------------------------------------------------------
    # QA fix loop
    # ------------------------------------------------------------------

    def _run_qa_loop(
        self,
        pr: PipelineRun,
        stage_def: StageDefinition,
        initial_artifact: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Delegate to QALoop; returns the (possibly improved) QA artifact."""
        from core.qa_loop import QALoop

        qa_loop = QALoop(
            agent_registry=self._agent_registry,
            config=self.config,
            db=self.db,
            build_agent_config_fn=self._build_agent_config,
            max_fix_iterations=self.config.get("qa", {}).get("max_fix_iterations", 3),
        )
        return qa_loop.run(pr, stage_def, initial_artifact)

    def _record_agent_tokens(
        self,
        run_id: str,
        stage: str,
        role: str,
        result: Any,
        model: str,
    ) -> None:
        """Persist per-agent token counts to the DB (best-effort; never raises)."""
        try:
            self.db.record_token_usage(
                run_id=run_id,
                stage=stage,
                role=role,
                tokens_in=getattr(result, "tokens_in", 0),
                tokens_out=getattr(result, "tokens_out", 0),
                duration_seconds=getattr(result, "duration_seconds", 0.0),
                model=model,
            )
        except Exception as exc:
            logger.debug("Token recording failed for %s/%s: %s", stage, role, exc)

    # ------------------------------------------------------------------
    # Task building helpers
    # ------------------------------------------------------------------

    def _build_development_tasks(
        self, stage_def: StageDefinition, pr: PipelineRun
    ) -> List[Dict[str, Any]]:
        """Build task specs for the parallel development stage.

        In a full implementation, tasks come from the planning artifact's
        task card list. This scaffold generates one task per role.
        """
        planning = pr.artifact_bundle.get_stage("planning") or {}
        task_cards = planning.get("tasks", []) if isinstance(planning, dict) else []

        tasks: List[Dict[str, Any]] = []

        # If planning produced explicit task cards, use them
        if task_cards:
            for card in task_cards:
                role = card.get("assignee_role", "developer_backend")
                if role not in stage_def.agent_roles:
                    continue
                task_id = card.get("task_id", f"t-{uuid.uuid4().hex[:6]}")
                self.db.create_task(
                    task_id=task_id,
                    run_id=pr.run_id,
                    title=card.get("title", ""),
                    assignee=role,
                    metadata=card,
                )
                tasks.append(
                    {
                        "role": role,
                        "task_id": task_id,
                        "description": (
                            f"## Task: {card.get('title', '')}\n\n"
                            f"{card.get('description', '')}\n\n"
                            f"## Acceptance Criteria\n"
                            + "\n".join(f"- {c}" for c in card.get("acceptance_criteria", []))
                        ),
                    }
                )
        else:
            # Fallback: one generic task per role in the stage
            for role in stage_def.agent_roles:
                task_id = f"{stage_def.name}-{role}-{uuid.uuid4().hex[:6]}"
                self.db.create_task(
                    task_id=task_id,
                    run_id=pr.run_id,
                    title=f"{role} task for {stage_def.name}",
                    assignee=role,
                )
                tasks.append(
                    {
                        "role": role,
                        "task_id": task_id,
                        "description": self._build_task_description(role, stage_def, pr),
                    }
                )

        return tasks

    def _build_task_description(
        self, role: str, stage_def: StageDefinition, pr: PipelineRun
    ) -> str:
        return (
            f"You are the {role} for this pipeline run.\n\n"
            f"## Original Idea\n{pr.idea}\n\n"
            f"## Your Task\n"
            f"Complete your responsibilities for the '{stage_def.name}' stage. "
            f"Produce a JSON artifact as described in your output contract."
        )

    def _build_agent_config(self, role: str, stage_def: StageDefinition, pr: PipelineRun):
        from core.agent_base import AgentConfig

        agent_cfg = self.config.get("agents", {}).get(role, {})
        pipeline_cfg = self.config.get("pipeline", {})
        compress_cfg = self.config.get("context_compression", {})

        # Per-tenant credentials override process env
        tenant_id = self.config.get("tenant_id", "default")
        api_key = os.getenv("OPENAI_API_KEY") or os.getenv("ANTHROPIC_API_KEY")
        base_url = os.getenv("OPENAI_BASE_URL") or os.getenv("ANTHROPIC_BASE_URL")
        if tenant_id != "default":
            try:
                from core.tenant import TenantConfig

                tc = TenantConfig.load(tenant_id)
                api_key = tc.api_key or api_key
                base_url = tc.base_url or base_url
            except Exception as exc:
                logger.debug("Tenant credential load failed: %s", exc)

        # Build optional context compressor
        compressor = None
        if compress_cfg.get("enabled"):
            try:
                from core.context_compressor import ContextCompressor

                compressor = ContextCompressor(
                    model=compress_cfg.get("model", "claude-haiku-4-5"),
                    threshold_tokens=int(compress_cfg.get("threshold_tokens", 30_000)),
                    keep_recent=int(compress_cfg.get("keep_recent", 6)),
                )
            except Exception as exc:
                logger.warning("ContextCompressor init failed: %s", exc)

        return AgentConfig(
            role=role,
            model=agent_cfg.get("model", pipeline_cfg.get("model", "claude-sonnet-4-6")),
            base_url=base_url,
            api_key=api_key,
            max_iterations=agent_cfg.get(
                "max_iterations",
                pipeline_cfg.get("max_iterations_per_agent", 30),
            ),
            enabled_toolsets=agent_cfg.get("toolsets", []),
            workspace=pr.workspace,
            compressor=compressor,
        )

    def _get_gate_config(self, stage_name: str) -> Dict[str, Any]:
        gates = self.config.get("governance", {}).get("hitl_gates", [])
        for gate in gates:
            if gate.get("stage") == stage_name:
                return gate
        return {"mode": self._default_gate_mode}

    # ------------------------------------------------------------------
    # Artifact disk persistence
    # ------------------------------------------------------------------

    def _write_artifact_to_disk(
        self, workspace: Path, stage: str, artifact: Dict[str, Any]
    ) -> None:
        """Submit an async disk write for the stage artifact — pipeline is never blocked.

        The artifact is serialised to JSON in the calling thread (cheap, in-memory)
        before being handed off, so the dict is safe to mutate afterwards.
        The single-worker executor serialises all writes; errors are logged, never raised.
        """
        payload = json.dumps(artifact, indent=2, ensure_ascii=False)

        def _write(dest_dir: Path, name: str, data: str) -> None:
            try:
                dest_dir.mkdir(parents=True, exist_ok=True)
                (dest_dir / name).write_text(data, encoding="utf-8")
                logger.info("Artifact written to disk: %s/%s", dest_dir, name)
            except Exception as exc:
                logger.warning("Artifact disk write failed (%s/%s): %s", dest_dir, name, exc)

        self._disk_writer.submit(_write, workspace / "artifacts", f"{stage}.json", payload)

    # ------------------------------------------------------------------
    # Git workspace helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _git_run(args: List[str], cwd: Path) -> bool:
        """Run a git command; return True on success, log and return False on failure."""
        import subprocess

        try:
            subprocess.run(
                ["git"] + args,
                cwd=str(cwd),
                capture_output=True,
                check=True,
            )
            return True
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            logger.debug("git %s failed: %s", " ".join(args), exc)
            return False

    def _git_init(self, workspace: Path, idea: str) -> None:
        """Initialise a git repo in the run workspace and make an initial commit."""
        if (workspace / ".git").exists():
            return
        if not self._git_run(["init"], workspace):
            return
        self._git_run(["config", "user.email", "sdlc-factory@local"], workspace)
        self._git_run(["config", "user.name", "SDLC Factory"], workspace)
        # Write a brief README so the initial commit is non-empty
        readme = workspace / "README.md"
        if not readme.exists():
            readme.write_text(f"# Pipeline run\n\n{idea}\n", encoding="utf-8")
        self._git_run(["add", "-A"], workspace)
        self._git_run(
            ["commit", "--allow-empty", "-m", "chore: init pipeline workspace"],
            workspace,
        )
        logger.info("git init completed for workspace %s", workspace)

    def _git_commit_stage(self, workspace: Path, stage_name: str) -> None:
        """Stage all changes and commit with the stage name as the message."""
        if not (workspace / ".git").exists():
            return
        self._git_run(["add", "-A"], workspace)
        # Only commit if there's something to commit
        import subprocess

        try:
            result = subprocess.run(
                ["git", "diff", "--cached", "--quiet"],
                cwd=str(workspace),
                capture_output=True,
            )
            if result.returncode == 0:
                return  # nothing staged
        except FileNotFoundError:
            return
        self._git_run(
            ["commit", "-m", f"feat: complete {stage_name} stage"],
            workspace,
        )
        logger.debug("git commit: stage %s", stage_name)

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_config(cls, config_path: str) -> "SDLCPipeline":
        import yaml

        with open(config_path, encoding="utf-8") as f:
            config = yaml.safe_load(f)
        return cls(config=config)

    @classmethod
    def from_dict(
        cls,
        config: Dict[str, Any],
        db: Optional[PipelineDB] = None,
        hitl_gateway: Optional[HITLGateway] = None,
    ) -> "SDLCPipeline":
        return cls(config=config, db=db, hitl_gateway=hitl_gateway)
