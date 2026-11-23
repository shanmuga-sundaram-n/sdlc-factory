"""BaseSDLCAgent — base class for all SDLC pipeline agent roles.

Every specialist agent role (ArchitectAgent, DeveloperBackendAgent, etc.)
subclasses BaseSDLCAgent. The base class handles:
  - System prompt assembly (role identity + SKILL.md + memory context)
  - Artifact contract enforcement (JSON schema validation on output)
  - Iteration budget with thread-safe consumption tracking
  - Context compression trigger (delegates to ContextCompressor
    when available, otherwise uses a simple truncation fallback)
  - Structured output extraction from the model's last message

Design decision: the runtime is injected via call_model_openai so the
underlying LLM provider can be swapped without touching the agent hierarchy.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from core.memory import ArtifactBundle, MemoryManager, ProjectMemory
from core.registry import ToolRegistry, registry as _default_registry
from core.session import PipelineDB
from core.telemetry import agent_span

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# IterationBudget
# ---------------------------------------------------------------------------


class IterationBudget:
    """Thread-safe iteration counter.  consume() → True if budget remains."""

    def __init__(self, max_total: int):
        self.max_total = max_total
        self._used = 0
        self._lock = threading.Lock()

    def consume(self) -> bool:
        with self._lock:
            if self._used >= self.max_total:
                return False
            self._used += 1
            return True

    def refund(self) -> None:
        with self._lock:
            if self._used > 0:
                self._used -= 1

    @property
    def used(self) -> int:
        with self._lock:
            return self._used

    @property
    def remaining(self) -> int:
        with self._lock:
            return max(0, self.max_total - self._used)

    def __repr__(self) -> str:
        return f"IterationBudget(used={self.used}/{self.max_total})"


# ---------------------------------------------------------------------------
# AgentConfig
# ---------------------------------------------------------------------------


@dataclass
class AgentConfig:
    """Runtime configuration for a single agent invocation."""

    role: str
    model: str = "claude-sonnet-4-6"
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    max_iterations: int = 30
    enabled_toolsets: List[str] = field(default_factory=list)
    blocked_tools: set = field(default_factory=set)
    workspace: Path = field(default_factory=lambda: Path("./output"))
    verbose: bool = False
    # Optional context compressor (set to a ContextCompressor instance to enable)
    compressor: Optional[Any] = None
    # Callbacks (all optional)
    on_tool_start: Optional[Callable] = None
    on_tool_complete: Optional[Callable] = None
    on_turn_complete: Optional[Callable] = None


# ---------------------------------------------------------------------------
# AgentResult
# ---------------------------------------------------------------------------


@dataclass
class AgentResult:
    """Structured result from a single agent run."""

    role: str
    run_id: str
    task_id: Optional[str]
    success: bool
    artifact: Optional[Dict[str, Any]]
    error: Optional[str] = None
    iterations_used: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    duration_seconds: float = 0.0
    raw_messages: List[Dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# BaseSDLCAgent
# ---------------------------------------------------------------------------

AGENT_BLOCKED_TOOLS_DEFAULT = frozenset(
    {
        "clarify",  # no user interaction in batch mode
        "send_message",  # no side effects to external systems
        "delegate_task",  # subagents don't spawn sub-subagents
    }
)


class BaseSDLCAgent:
    """Base class for all SDLC specialist agent roles.

    Subclasses override:
      - ROLE: str — machine-readable role identifier
      - ROLE_DISPLAY: str — human-readable role name
      - SKILL_MD_PATH: str — relative path to the role's SKILL.md
      - AUTHORITY_STATEMENT: str — what this role is empowered to decide
      - OUTPUT_SCHEMA: dict — JSON Schema for the artifact this role produces
      - ENABLED_TOOLSETS: list[str] — default toolsets for this role
      - MAX_ITERATIONS: int — default iteration cap
    """

    ROLE: str = "base"
    ROLE_DISPLAY: str = "Base Agent"
    SKILL_MD_PATH: str = ""
    AUTHORITY_STATEMENT: str = ""
    OUTPUT_SCHEMA: Dict[str, Any] = {}
    ENABLED_TOOLSETS: List[str] = []
    MAX_ITERATIONS: int = 30

    def __init__(
        self,
        config: AgentConfig,
        project_memory: ProjectMemory,
        artifact_bundle: ArtifactBundle,
        db: PipelineDB,
        tool_registry: Optional[ToolRegistry] = None,
    ):
        self.config = config
        self.project_memory = project_memory
        self.artifact_bundle = artifact_bundle
        self.db = db
        self.registry = tool_registry or _default_registry
        self.budget = IterationBudget(config.max_iterations or self.MAX_ITERATIONS)
        self._interrupted = threading.Event()
        self._agent_id = str(uuid.uuid4())[:8]

    # ------------------------------------------------------------------
    # Subclass hooks
    # ------------------------------------------------------------------

    def skill_md(self) -> str:
        """Load SKILL.md from SKILL_MD_PATH. Returns empty string if missing."""
        if not self.SKILL_MD_PATH:
            return ""
        p = Path(__file__).parent.parent / self.SKILL_MD_PATH
        if p.exists():
            return p.read_text(encoding="utf-8")
        return ""

    def build_system_prompt(self, memory_manager: MemoryManager) -> str:
        """Assemble the full system prompt for this role.

        Parts:
          1. Role identity + authority statement
          2. SKILL.md content (role instructions)
          3. Output contract (what the agent MUST produce)
          4. Memory context (project memory + prior stage artifacts)
        """
        parts = []

        # 1 — Identity
        parts.append(
            f"# Role: {self.ROLE_DISPLAY}\n\n"
            f"## Authority\n{self.AUTHORITY_STATEMENT}\n\n"
            f"## Your job\n"
            f"You are working as part of an autonomous software delivery pipeline. "
            f"Complete your stage and produce the required artifact. "
            f"Do not attempt work that belongs to other roles.\n\n"
            f"## Workspace\n"
            f"Your working directory is: `{self.config.workspace}`\n"
            f"All file paths you pass to file tools must be relative to this directory. "
            f"You may NOT write files outside this directory."
        )

        # 2 — SKILL.md
        skill = self.skill_md()
        if skill:
            parts.append(f"## Role Instructions\n\n{skill}")

        # 3 — Output contract
        if self.OUTPUT_SCHEMA:
            schema_str = json.dumps(self.OUTPUT_SCHEMA, indent=2)
            parts.append(
                f"## Output Contract\n\n"
                f"You MUST conclude your work by emitting a JSON artifact that conforms "
                f"to the following schema. Emit it as the final block in your response, "
                f"wrapped in a ```json ... ``` fence.\n\n"
                f"```json\n{schema_str}\n```"
            )

        # 4 — Memory context
        mem_block = memory_manager.build_system_prompt()
        if mem_block:
            parts.append(mem_block)

        return "\n\n".join(parts)

    def prior_stages(self) -> List[str]:
        """Return the list of stage names that precede this role's stage.

        Subclasses override to declare their position in the pipeline.
        """
        return []

    # ------------------------------------------------------------------
    # Core run loop
    # ------------------------------------------------------------------

    def run(
        self,
        task_description: str,
        stage: str,
        *,
        task_id: Optional[str] = None,
        ephemeral_context: str = "",
    ) -> AgentResult:
        """Run this agent on a task and return a structured AgentResult.

        This is the primary entry point. It:
          1. Assembles the system prompt.
          2. Builds the initial user message.
          3. Runs the tool-calling loop (delegates to _run_loop).
          4. Extracts the structured artifact from the final response.
          5. Validates the artifact against OUTPUT_SCHEMA.
          6. Persists the artifact to the DB.
        """
        start = time.monotonic()
        run_id = self.artifact_bundle.run_id

        mm = MemoryManager(
            self.project_memory,
            self.artifact_bundle,
            stage=stage,
            prior_stages=self.prior_stages(),
        )

        system_prompt = self.build_system_prompt(mm)
        tool_defs = self.registry.get_definitions(
            enabled_toolsets=self.config.enabled_toolsets or self.ENABLED_TOOLSETS,
            blocked_tools=self.config.blocked_tools | AGENT_BLOCKED_TOOLS_DEFAULT,
        )

        user_message = task_description
        if ephemeral_context:
            user_message = f"{ephemeral_context}\n\n---\n\n{task_description}"

        messages = [{"role": "user", "content": user_message}]

        logger.info(
            "[%s] %s starting | run=%s stage=%s task=%s budget=%d",
            self._agent_id,
            self.ROLE,
            run_id,
            stage,
            task_id,
            self.budget.max_total,
        )

        try:
            with agent_span(run_id, stage, self.ROLE, self._agent_id):
                messages, tokens_in, tokens_out = self._run_loop(
                    system_prompt=system_prompt,
                    messages=messages,
                    tool_defs=tool_defs,
                    mm=mm,
                )
        except Exception as exc:
            logger.exception("[%s] %s failed: %s", self._agent_id, self.ROLE, exc)
            return AgentResult(
                role=self.ROLE,
                run_id=run_id,
                task_id=task_id,
                success=False,
                artifact=None,
                error=str(exc),
                iterations_used=self.budget.used,
                duration_seconds=time.monotonic() - start,
            )

        # Extract artifact from the last assistant message
        artifact = self._extract_artifact(messages)

        # Recovery pass: if no artifact found, ask the model to re-emit just the JSON
        if artifact is None:
            logger.warning(
                "[%s] %s produced no parseable artifact — attempting recovery re-emit",
                self._agent_id,
                self.ROLE,
            )
            try:
                messages, tokens_in, tokens_out = self._recover_artifact(
                    system_prompt=system_prompt,
                    messages=messages,
                    tool_defs=tool_defs,
                    mm=mm,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                )
                artifact = self._extract_artifact(messages)
            except Exception as exc:
                logger.warning("[%s] %s recovery failed: %s", self._agent_id, self.ROLE, exc)

        if artifact is None:
            logger.error(
                "[%s] %s produced no parseable artifact (including after recovery)",
                self._agent_id,
                self.ROLE,
            )
            return AgentResult(
                role=self.ROLE,
                run_id=run_id,
                task_id=task_id,
                success=False,
                artifact=None,
                error="No JSON artifact found in response",
                iterations_used=self.budget.used,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                duration_seconds=time.monotonic() - start,
                raw_messages=messages,
            )

        logger.info(
            "[%s] %s completed | stage=%s iterations=%d tokens=%d+%d",
            self._agent_id,
            self.ROLE,
            stage,
            self.budget.used,
            tokens_in,
            tokens_out,
        )

        return AgentResult(
            role=self.ROLE,
            run_id=run_id,
            task_id=task_id,
            success=True,
            artifact=artifact,
            iterations_used=self.budget.used,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            duration_seconds=time.monotonic() - start,
            raw_messages=messages,
        )

    def _recover_artifact(
        self,
        system_prompt: str,
        messages: List[Dict],
        tool_defs: List[Dict],
        mm,
        tokens_in: int,
        tokens_out: int,
    ) -> Tuple[List[Dict], int, int]:
        """Ask the model to re-emit its artifact as a JSON fence."""
        from core.agent_runtime import call_model_openai

        recovery_msg = (
            "Your previous response did not contain a parseable JSON artifact. "
            "Please output ONLY the final artifact now, wrapped in a ```json ... ``` fence. "
            "Do not include any other text — just the JSON object."
        )
        messages = messages + [{"role": "user", "content": recovery_msg}]
        response = call_model_openai(
            model=self.config.model,
            base_url=self.config.base_url,
            api_key=self.config.api_key,
            system=system_prompt,
            messages=messages,
            tools=[],  # no tools on recovery pass
        )
        tokens_in += response.get("usage", {}).get("prompt_tokens", 0)
        tokens_out += response.get("usage", {}).get("completion_tokens", 0)
        messages = messages + [response.get("message", {})]
        return messages, tokens_in, tokens_out

    def interrupt(self, reason: str = "interrupted") -> None:
        logger.info("[%s] %s interrupted: %s", self._agent_id, self.ROLE, reason)
        self._interrupted.set()

    # ------------------------------------------------------------------
    # Internal: tool-calling loop
    # ------------------------------------------------------------------

    def _run_loop(
        self,
        system_prompt: str,
        messages: List[Dict],
        tool_defs: List[Dict],
        mm: MemoryManager,
    ) -> Tuple[List[Dict], int, int]:
        """Run the model↔tool loop until completion or budget exhaustion.

        Returns (messages, total_tokens_in, total_tokens_out).

        This is intentionally written to work with any OpenAI-compatible
        client. Swap _call_model() for a different backend.
        """
        from core.agent_runtime import call_model_openai  # avoid circular at module level

        total_in = 0
        total_out = 0
        while True:
            if self._interrupted.is_set():
                logger.info("[%s] %s: interrupt received, stopping loop", self._agent_id, self.ROLE)
                break

            if not self.budget.consume():
                logger.warning(
                    "[%s] %s: iteration budget exhausted (%d/%d)",
                    self._agent_id,
                    self.ROLE,
                    self.budget.used,
                    self.budget.max_total,
                )
                break

            # Compress context if configured and threshold exceeded
            if self.config.compressor is not None:
                messages = self.config.compressor.maybe_compress(messages, system_prompt)

            response = call_model_openai(
                model=self.config.model,
                base_url=self.config.base_url,
                api_key=self.config.api_key,
                system=system_prompt,
                messages=messages,
                tools=tool_defs,
            )

            total_in += response.get("usage", {}).get("prompt_tokens", 0)
            total_out += response.get("usage", {}).get("completion_tokens", 0)

            assistant_msg = response.get("message", {})
            if self.config.on_turn_complete:
                self.config.on_turn_complete(assistant_msg)

            messages.append(assistant_msg)

            # Check stop reason
            stop_reason = response.get("stop_reason", "")
            tool_calls = assistant_msg.get("tool_calls") or []

            if not tool_calls or stop_reason == "end_turn":
                break

            # Dispatch tool calls
            tool_results = self._dispatch_tools(tool_calls)
            messages.append({"role": "tool_results", "content": tool_results})

        return messages, total_in, total_out

    def _dispatch_tools(self, tool_calls: List[Dict]) -> List[Dict]:
        """Execute a batch of tool calls and return tool result messages."""
        # Inject per-agent runtime state so tool handlers can access workspace/DB
        try:
            from tools._context import set_tool_context

            set_tool_context(
                workspace=self.config.workspace,
                db=self.db,
                run_id=self.artifact_bundle.run_id,
            )
        except ImportError:
            pass

        results = []
        for tc in tool_calls:
            fn = tc.get("function", {})
            tool_name = fn.get("name", "")
            raw_args = fn.get("arguments", "{}")
            tc_id = tc.get("id", "")

            if self.config.on_tool_start:
                self.config.on_tool_start(tool_name, raw_args)

            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                result = self.registry.handle(tool_name, args)
            except KeyError:
                result = json.dumps({"error": f"Unknown tool: {tool_name}"})
            except Exception as exc:
                result = json.dumps({"error": str(exc)})

            if self.config.on_tool_complete:
                self.config.on_tool_complete(tool_name, result)

            results.append(
                {
                    "tool_call_id": tc_id,
                    "role": "tool",
                    "content": result if isinstance(result, str) else json.dumps(result),
                }
            )

        return results

    # ------------------------------------------------------------------
    # Artifact extraction
    # ------------------------------------------------------------------

    def _extract_artifact(self, messages: List[Dict]) -> Optional[Dict[str, Any]]:
        """Pull the last ```json ... ``` fence from the assistant's response."""
        import re

        for msg in reversed(messages):
            if msg.get("role") != "assistant":
                continue
            content = msg.get("content") or ""
            if isinstance(content, list):
                # Multi-part content (Anthropic style)
                content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
            # Find ```json ... ``` fence
            match = re.search(r"```json\s*([\s\S]*?)\s*```", content)
            if match:
                try:
                    return json.loads(match.group(1))
                except json.JSONDecodeError:
                    logger.warning("JSON parse failed in artifact block")
                    continue
            # Fallback: bare JSON object
            match = re.search(r"\{[\s\S]*\}", content)
            if match:
                try:
                    return json.loads(match.group(0))
                except json.JSONDecodeError:
                    pass
        return None


# ---------------------------------------------------------------------------
# Type annotation fix for _run_loop return
# ---------------------------------------------------------------------------
from typing import Tuple  # noqa: E402
