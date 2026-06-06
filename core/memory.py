"""Shared memory and inter-agent context for the SDLC pipeline.

Three-tier context system:
  Tier 1 — Project Glossary (PROJECT_MEMORY.md) — injected into all agents.
  Tier 2 — Stage Artifacts (SQLite) — injected as a fenced context block.
  Tier 3 — Per-swarm Blackboard (task comments in kanban) — ephemeral.

StreamingContextScrubber strips <stage-context> fences from model output
so they never surface to the user or leak into the next turn.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Context fence tags
# ---------------------------------------------------------------------------

_STAGE_CONTEXT_TAG = "stage-context"
_FENCE_OPEN = f"<{_STAGE_CONTEXT_TAG}>"
_FENCE_CLOSE = f"</{_STAGE_CONTEXT_TAG}>"

_FENCE_TAG_RE = re.compile(rf'</?\s*{re.escape(_STAGE_CONTEXT_TAG)}\s*>', re.IGNORECASE)
_INTERNAL_CONTEXT_RE = re.compile(
    rf'<\s*{re.escape(_STAGE_CONTEXT_TAG)}\s*>[\s\S]*?</\s*{re.escape(_STAGE_CONTEXT_TAG)}\s*>',
    re.IGNORECASE,
)
_SYSTEM_NOTE_RE = re.compile(
    r'\[System note:\s*The following is stage context[\s\S]*?\]\s*',
    re.IGNORECASE,
)


def sanitize_context(text: str) -> str:
    """Strip fence tags and injected context blocks from provider output."""
    text = _INTERNAL_CONTEXT_RE.sub("", text)
    text = _SYSTEM_NOTE_RE.sub("", text)
    text = _FENCE_TAG_RE.sub("", text)
    return text


# ---------------------------------------------------------------------------
# Prompt injection scanner — applied to the user-supplied idea string
# ---------------------------------------------------------------------------

class PromptInjectionError(ValueError):
    """Raised when the idea string contains prompt injection patterns."""


_INJECTION_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("role_override", re.compile(
        r'\b(ignore|disregard|forget|override)\b.{0,40}\b(previous|prior|above|your)\b'
        r'.{0,40}\b(instructions?|prompt|rules?|constraints?)\b',
        re.IGNORECASE | re.DOTALL,
    )),
    ("persona_hijack", re.compile(
        r'\b(you are now|act as|pretend (to be|you are)|your (new )?role is|from now on you)\b',
        re.IGNORECASE,
    )),
    ("system_role_injection", re.compile(
        r'(?m)^\s*(system|user|assistant)\s*:\s*\S',
        re.IGNORECASE,
    )),
    ("fence_tag_injection", re.compile(
        rf'<\s*{re.escape(_STAGE_CONTEXT_TAG)}',
        re.IGNORECASE,
    )),
    ("jailbreak_keyword", re.compile(
        r'\b(jailbreak|developer mode|DAN mode|do anything now)\b',
        re.IGNORECASE,
    )),
    ("prompt_exfiltration", re.compile(
        r'\b(reveal|print|show|output|repeat|tell me)\b.{0,30}'
        r'\b(your (system )?prompt|your instructions?|above text)\b',
        re.IGNORECASE | re.DOTALL,
    )),
]


def scan_for_injection(text: str) -> None:
    """Scan user-supplied input for prompt injection patterns.

    Raises PromptInjectionError naming the matched pattern if suspicious
    content is detected. Call this on the raw idea string before any
    pipeline state is created.
    """
    for name, pattern in _INJECTION_PATTERNS:
        if pattern.search(text):
            raise PromptInjectionError(
                f"Prompt injection detected ({name}) — idea string contains disallowed content."
            )


# ---------------------------------------------------------------------------
# StreamingContextScrubber
# Strips <stage-context>…</stage-context> spans from a streaming token stream,
# surviving chunk boundaries.
# ---------------------------------------------------------------------------

class StreamingContextScrubber:
    def __init__(self) -> None:
        self._in_span: bool = False
        self._buf: str = ""

    def reset(self) -> None:
        self._in_span = False
        self._buf = ""

    def feed(self, text: str) -> str:
        if not text:
            return ""
        buf = self._buf + text
        self._buf = ""
        out: list[str] = []

        while buf:
            if self._in_span:
                idx = buf.lower().find(_FENCE_CLOSE)
                if idx == -1:
                    held = self._max_partial_suffix(buf, _FENCE_CLOSE)
                    self._buf = buf[-held:] if held else ""
                    return "".join(out)
                buf = buf[idx + len(_FENCE_CLOSE):]
                self._in_span = False
            else:
                idx = buf.lower().find(_FENCE_OPEN)
                if idx == -1:
                    held = self._max_partial_suffix(buf, _FENCE_OPEN)
                    if held:
                        out.append(buf[:-held])
                        self._buf = buf[-held:]
                    else:
                        out.append(buf)
                    return "".join(out)
                if idx > 0:
                    out.append(buf[:idx])
                buf = buf[idx + len(_FENCE_OPEN):]
                self._in_span = True

        return "".join(out)

    def flush(self) -> str:
        if self._in_span:
            self._buf = ""
            self._in_span = False
            return ""
        tail = self._buf
        self._buf = ""
        return tail

    @staticmethod
    def _max_partial_suffix(buf: str, tag: str) -> int:
        tag_lower = tag.lower()
        buf_lower = buf.lower()
        max_check = min(len(buf_lower), len(tag_lower) - 1)
        for i in range(max_check, 0, -1):
            if tag_lower.startswith(buf_lower[-i:]):
                return i
        return 0


# ---------------------------------------------------------------------------
# ProjectMemory — Tier 1 (persistent, shared across all agents)
# ---------------------------------------------------------------------------

class ProjectMemory:
    """Reads/writes PROJECT_MEMORY.md in the workspace.

    All agents receive this content injected into their system prompt.
    Only authorised agents (DomainExpert, PO, BA) may write to it.
    """

    def __init__(self, workspace: Path):
        self._path = workspace / "PROJECT_MEMORY.md"
        self._lock = threading.RLock()

    def read(self) -> str:
        with self._lock:
            if not self._path.exists():
                return ""
            return self._path.read_text(encoding="utf-8")

    def write(self, content: str) -> None:
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(content, encoding="utf-8")
            logger.info("ProjectMemory updated (%d chars)", len(content))

    def append(self, section_title: str, content: str) -> None:
        with self._lock:
            existing = self.read()
            separator = "\n\n---\n\n" if existing else ""
            new_content = f"{existing}{separator}## {section_title}\n\n{content}"
            self.write(new_content)

    def build_system_prompt_block(self) -> str:
        content = self.read()
        if not content:
            return ""
        return (
            "[System note: The following is the project memory shared across all agents. "
            "Treat it as authoritative reference data.]\n\n"
            f"{_FENCE_OPEN}\n{content}\n{_FENCE_CLOSE}"
        )


# ---------------------------------------------------------------------------
# ArtifactBundle — Tier 2 (per-stage artifacts injected as context)
# ---------------------------------------------------------------------------

class ArtifactBundle:
    """Accumulates structured artifacts from each pipeline stage.

    Each agent receives the bundle injected as a fenced <stage-context> block.
    The streaming scrubber strips it from the model's output before display.
    """

    def __init__(self, run_id: str):
        self.run_id = run_id
        self._artifacts: Dict[str, Any] = {"run_id": run_id}
        self._lock = threading.RLock()

    def set_stage(self, stage: str, artifact: Any) -> None:
        with self._lock:
            self._artifacts[stage] = artifact

    def get_stage(self, stage: str) -> Optional[Any]:
        with self._lock:
            return self._artifacts.get(stage)

    def as_dict(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._artifacts)

    def build_context_block(self, for_stage: str, prior_stages: List[str]) -> str:
        """Build the injected context block an agent sees at the start of its run.

        Only includes artifacts from prior_stages (not the current one).
        """
        with self._lock:
            visible = {k: v for k, v in self._artifacts.items() if k in prior_stages}

        if not visible:
            return ""

        payload = json.dumps(visible, indent=2, ensure_ascii=False)
        return (
            f"[System note: The following is the accumulated pipeline artifact bundle "
            f"for run {self.run_id!r}. "
            f"You are working on stage {for_stage!r}. "
            f"Earlier stage outputs are provided as reference — do NOT re-do work "
            f"that is already captured here.]\n\n"
            f"{_FENCE_OPEN}\n{payload}\n{_FENCE_CLOSE}"
        )

    @classmethod
    def from_dict(cls, data: dict) -> "ArtifactBundle":
        run_id = data.get("run_id", "unknown")
        bundle = cls(run_id=run_id)
        for k, v in data.items():
            bundle._artifacts[k] = v
        return bundle


# ---------------------------------------------------------------------------
# MemoryManager — orchestrates both tiers for a single agent invocation
# ---------------------------------------------------------------------------

class MemoryManager:
    """Single integration point in BaseSDLCAgent.

    Assembles the system prompt fragment (Tier 1 + Tier 2) and provides
    the streaming scrubber for output filtering.

    Usage in BaseSDLCAgent:
        mm = MemoryManager(project_memory, artifact_bundle, stage="architecture",
                           prior_stages=["discovery", "requirements"])
        extra_system = mm.build_system_prompt()
        scrubber = mm.make_scrubber()
        # ... stream model response tokens through scrubber.feed(delta)
    """

    def __init__(
        self,
        project_memory: ProjectMemory,
        artifact_bundle: ArtifactBundle,
        stage: str,
        prior_stages: List[str],
    ):
        self._pm = project_memory
        self._bundle = artifact_bundle
        self._stage = stage
        self._prior_stages = prior_stages

    def build_system_prompt(self) -> str:
        parts = []
        pm_block = self._pm.build_system_prompt_block()
        if pm_block:
            parts.append(pm_block)
        artifact_block = self._bundle.build_context_block(self._stage, self._prior_stages)
        if artifact_block:
            parts.append(artifact_block)
        return "\n\n".join(parts)

    def make_scrubber(self) -> StreamingContextScrubber:
        return StreamingContextScrubber()

    def update_project_memory(self, section_title: str, content: str) -> None:
        self._pm.append(section_title, content)
