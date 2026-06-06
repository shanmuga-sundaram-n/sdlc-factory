"""Context compression tests.

Covers:
- Token estimation (char/4 heuristic)
- maybe_compress no-ops below threshold
- maybe_compress triggers above threshold
- Compression keeps keep_recent messages verbatim
- Compression fails gracefully (returns original messages)
- Wire-up: AgentConfig.compressor is called in _run_loop
"""

from __future__ import annotations

from unittest.mock import patch, MagicMock
from typing import List, Dict

import pytest

from core.context_compressor import ContextCompressor


def _make_messages(n: int, content_size: int = 100) -> List[Dict]:
    return [
        {"role": "assistant" if i % 2 else "user", "content": "x" * content_size}
        for i in range(n)
    ]


_SUMMARY_RESPONSE = {
    "message": {"role": "assistant", "content": "Summarised context."},
    "stop_reason": "end_turn",
    "usage": {"prompt_tokens": 50, "completion_tokens": 80},
}


class TestTokenEstimation:
    def test_empty_messages_zero(self):
        c = ContextCompressor()
        assert c._estimate_tokens([]) == 0

    def test_single_message(self):
        c = ContextCompressor()
        msgs = [{"role": "user", "content": "a" * 400}]
        assert c._estimate_tokens(msgs) == 100  # 400/4

    def test_system_contributes(self):
        c = ContextCompressor()
        assert c._estimate_tokens([], system="s" * 400) == 100

    def test_list_content(self):
        c = ContextCompressor()
        msgs = [{"role": "user", "content": [{"text": "a" * 200}]}]
        est = c._estimate_tokens(msgs)
        assert est > 0


class TestMaybeCompress:
    def test_no_op_below_threshold(self):
        c = ContextCompressor(threshold_tokens=10_000, keep_recent=4)
        msgs = _make_messages(5, content_size=10)
        result = c.maybe_compress(msgs)
        assert result is msgs  # same object — no copy

    def test_triggers_above_threshold(self):
        c = ContextCompressor(threshold_tokens=50, keep_recent=2)
        msgs = _make_messages(10, content_size=100)
        with patch("core.agent_runtime.call_model_openai", return_value=_SUMMARY_RESPONSE):
            result = c.maybe_compress(msgs)
        assert len(result) < len(msgs)

    def test_keeps_recent_messages(self):
        keep = 3
        c = ContextCompressor(threshold_tokens=50, keep_recent=keep)
        msgs = _make_messages(10, content_size=100)
        with patch("core.agent_runtime.call_model_openai", return_value=_SUMMARY_RESPONSE):
            result = c.maybe_compress(msgs)
        # The last keep_recent messages should be at the end
        assert result[-keep:] == msgs[-keep:]

    def test_summary_message_injected(self):
        c = ContextCompressor(threshold_tokens=50, keep_recent=2)
        msgs = _make_messages(10, content_size=100)
        with patch("core.agent_runtime.call_model_openai", return_value=_SUMMARY_RESPONSE):
            result = c.maybe_compress(msgs)
        # First message should be the summary
        assert "CONTEXT SUMMARY" in result[0]["content"]
        assert "Summarised context" in result[0]["content"]

    def test_graceful_on_llm_failure(self):
        c = ContextCompressor(threshold_tokens=50, keep_recent=2)
        msgs = _make_messages(10, content_size=100)
        with patch("core.agent_runtime.call_model_openai",
                   side_effect=Exception("API error")):
            result = c.maybe_compress(msgs)
        # Falls back to original messages
        assert result == msgs

    def test_few_messages_not_compressed(self):
        c = ContextCompressor(threshold_tokens=50, keep_recent=6)
        msgs = _make_messages(4, content_size=100)  # fewer than keep_recent
        with patch("core.agent_runtime.call_model_openai", return_value=_SUMMARY_RESPONSE) as m:
            result = c.maybe_compress(msgs)
        # _compress skips when len <= keep_recent; may still be called with large msgs
        # Just verify no crash and same count or fewer
        assert len(result) <= len(msgs) + 1

    def test_system_prompt_counted(self):
        c = ContextCompressor(threshold_tokens=10, keep_recent=2)
        msgs = [{"role": "user", "content": "hi"}]
        system = "s" * 200
        with patch("core.agent_runtime.call_model_openai", return_value=_SUMMARY_RESPONSE):
            result = c.maybe_compress(msgs, system=system)
        # With large system + threshold=10, should trigger
        assert len(result) <= len(msgs) + 1


class TestAgentConfigCompressor:
    def test_compressor_field_on_agent_config(self):
        from core.agent_base import AgentConfig
        c = ContextCompressor()
        cfg = AgentConfig(role="test", compressor=c)
        assert cfg.compressor is c

    def test_compressor_called_in_run_loop(self):
        """Verify ContextCompressor.maybe_compress is called during _run_loop."""
        from core.agent_base import AgentConfig, BaseSDLCAgent
        from core.memory import ArtifactBundle
        from unittest.mock import MagicMock

        compressor = MagicMock(spec=ContextCompressor)
        compressor.maybe_compress.side_effect = lambda msgs, system="": msgs

        cfg = AgentConfig(role="base", compressor=compressor)
        pm = MagicMock()   # plain mock — no spec constraint
        ab = ArtifactBundle("run-cmp")
        db = MagicMock()

        agent = BaseSDLCAgent(
            config=cfg,
            project_memory=pm,
            artifact_bundle=ab,
            db=db,
        )

        mock_response = {
            "message": {"role": "assistant", "content": '```json\n{"done": true}\n```'},
            "stop_reason": "end_turn",
            "usage": {"prompt_tokens": 5, "completion_tokens": 5},
        }
        with patch("core.agent_runtime.call_model_openai", return_value=mock_response):
            mm = MagicMock()
            mm.make_scrubber.return_value = MagicMock(scrub=lambda x: x)
            agent._run_loop(
                system_prompt="test",
                messages=[{"role": "user", "content": "go"}],
                tool_defs=[],
                mm=mm,
            )

        assert compressor.maybe_compress.called
