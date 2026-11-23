"""Context compression — condenses long conversation histories via LLM summarisation.

When an agent's tool-calling loop accumulates too many tokens, the compressor:
  1. Estimates the current token count (char/4 heuristic)
  2. Keeps the N most-recent messages intact (they carry immediate context)
  3. Summarises the older messages using a cheap haiku call
  4. Replaces the old messages with a single [CONTEXT SUMMARY] message

Wire-up in AgentConfig:
    config.compressor = ContextCompressor()

Then in _run_loop, before each model call:
    if self.config.compressor:
        messages = self.config.compressor.maybe_compress(messages, system_prompt)
"""

from __future__ import annotations

import logging
from typing import Dict, List

logger = logging.getLogger(__name__)


class ContextCompressor:
    """Compresses agent message histories when they exceed a token threshold.

    Args:
        model: LLM to use for summarisation — defaults to haiku (fast + cheap).
        threshold_tokens: Estimated tokens above which compression triggers.
        keep_recent: Number of recent messages to keep verbatim after compression.
    """

    def __init__(
        self,
        model: str = "claude-haiku-4-5",
        threshold_tokens: int = 30_000,
        keep_recent: int = 6,
    ):
        self.model = model
        self.threshold_tokens = threshold_tokens
        self.keep_recent = keep_recent

    def maybe_compress(
        self,
        messages: List[Dict],
        system: str = "",
    ) -> List[Dict]:
        """Compress *messages* if estimated token count exceeds the threshold.

        Returns the original list untouched when compression is not needed.
        Never raises — on failure, returns the original messages.
        """
        estimated = self._estimate_tokens(messages, system)
        if estimated < self.threshold_tokens:
            return messages

        logger.info(
            "Context compressor triggered: ~%d estimated tokens (threshold=%d)",
            estimated,
            self.threshold_tokens,
        )
        try:
            return self._compress(messages, system)
        except Exception as exc:
            logger.warning("Context compression failed (%s); keeping original messages", exc)
            return messages

    def _estimate_tokens(self, messages: List[Dict], system: str = "") -> int:
        """Rough char/4 token estimate (good enough for triggering compression)."""
        total = len(system)
        for m in messages:
            content = m.get("content") or ""
            if isinstance(content, list):
                total += sum(len(str(p)) for p in content)
            else:
                total += len(str(content))
        return total // 4

    def _compress(self, messages: List[Dict], system: str = "") -> List[Dict]:
        from core.agent_runtime import call_model_openai

        if len(messages) <= self.keep_recent:
            return messages

        to_compress = messages[: -self.keep_recent]
        recent = messages[-self.keep_recent :]

        # Build summary input — cap at 8k chars to avoid recursive over-length
        lines = []
        for m in to_compress:
            role = m.get("role", "?")
            content = m.get("content") or ""
            if isinstance(content, list):
                content = " ".join(str(p) for p in content)
            lines.append(f"{role.upper()}: {str(content)[:400]}")
        conv_text = "\n".join(lines)[:8_000]

        summary_prompt = (
            "Summarise this AI agent conversation history concisely. "
            "Focus on: decisions made, files written, tools called, key findings. "
            "Keep it under 300 words.\n\n"
            f"{conv_text}"
        )

        response = call_model_openai(
            model=self.model,
            messages=[{"role": "user", "content": summary_prompt}],
            max_tokens=512,
        )
        summary_text = response.get("message", {}).get("content", "(summary unavailable)")

        summary_msg: Dict = {
            "role": "user",
            "content": (
                f"[CONTEXT SUMMARY — {len(to_compress)} messages compressed]\n\n" f"{summary_text}"
            ),
        }

        compressed = [summary_msg] + recent
        logger.info(
            "Compressed %d messages → 1 summary + %d recent (was ~%d tokens)",
            len(to_compress),
            len(recent),
            self._estimate_tokens(messages, system),
        )
        return compressed
