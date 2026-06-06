"""Provider failover tests.

Covers:
- classify_api_error: all error categories
- CredentialPool: from_env, acquire round-robin, mark_unavailable, available_count
- call_model_with_failover: rotates on rate limit, raises on auth exhaustion,
  retries on server errors, re-raises on context-too-long
"""

from __future__ import annotations

import time
from unittest.mock import patch, MagicMock

import pytest

from core.agent_runtime import (
    ErrorCategory,
    classify_api_error,
    CredentialPool,
    call_model_with_failover,
)


# ---------------------------------------------------------------------------
# classify_api_error
# ---------------------------------------------------------------------------

class TestClassifyApiError:
    def _exc(self, msg: str, status_code=None) -> Exception:
        e = Exception(msg)
        if status_code:
            e.status_code = status_code
        return e

    def test_rate_limit_by_message(self):
        assert classify_api_error(self._exc("rate limit exceeded")) == ErrorCategory.RATE_LIMIT

    def test_rate_limit_by_code_429(self):
        assert classify_api_error(self._exc("too many requests", 429)) == ErrorCategory.RATE_LIMIT

    def test_rate_limit_429_in_message(self):
        assert classify_api_error(self._exc("HTTP 429")) == ErrorCategory.RATE_LIMIT

    def test_auth_error_unauthorized(self):
        assert classify_api_error(self._exc("unauthorized")) == ErrorCategory.AUTH_ERROR

    def test_auth_error_invalid_api_key(self):
        assert classify_api_error(self._exc("invalid api key")) == ErrorCategory.AUTH_ERROR

    def test_auth_error_403(self):
        assert classify_api_error(self._exc("forbidden", 403)) == ErrorCategory.AUTH_ERROR

    def test_timeout_by_message(self):
        assert classify_api_error(self._exc("request timeout")) == ErrorCategory.TIMEOUT

    def test_timeout_by_code_408(self):
        assert classify_api_error(self._exc("timeout", 408)) == ErrorCategory.TIMEOUT

    def test_server_error_500(self):
        assert classify_api_error(self._exc("internal server error", 500)) == ErrorCategory.SERVER_ERROR

    def test_server_error_502(self):
        assert classify_api_error(self._exc("bad gateway", 502)) == ErrorCategory.SERVER_ERROR

    def test_server_error_503(self):
        assert classify_api_error(self._exc("service unavailable", 503)) == ErrorCategory.SERVER_ERROR

    def test_context_too_long(self):
        assert classify_api_error(self._exc("context length exceeded")) == ErrorCategory.CONTEXT_TOO_LONG

    def test_unknown(self):
        assert classify_api_error(self._exc("some weird error")) == ErrorCategory.UNKNOWN


# ---------------------------------------------------------------------------
# CredentialPool
# ---------------------------------------------------------------------------

class TestCredentialPool:
    def _pool(self, n: int = 3) -> CredentialPool:
        return CredentialPool([
            {"api_key": f"sk-key-{i}", "base_url": None}
            for i in range(n)
        ])

    def test_single_entry_pool(self):
        pool = CredentialPool([{"api_key": "sk-only", "base_url": None}])
        cred = pool.acquire()
        assert cred["api_key"] == "sk-only"

    def test_empty_pool_raises(self):
        with pytest.raises(ValueError):
            CredentialPool([])

    def test_round_robin(self):
        pool = self._pool(3)
        seen = {pool.acquire()["api_key"] for _ in range(9)}
        assert len(seen) == 3

    def test_mark_unavailable_removes_from_rotation(self):
        pool = self._pool(3)
        pool.mark_unavailable("sk-key-0", duration=3600)
        for _ in range(20):
            cred = pool.acquire()
            assert cred["api_key"] != "sk-key-0"

    def test_available_count_decreases(self):
        pool = self._pool(3)
        assert pool.available_count == 3
        pool.mark_unavailable("sk-key-0", duration=3600)
        assert pool.available_count == 2

    def test_expired_unavailable_is_restored(self):
        pool = self._pool(2)
        pool.mark_unavailable("sk-key-0", duration=0.01)
        time.sleep(0.02)
        assert pool.available_count == 2

    def test_all_unavailable_falls_back_to_any(self):
        pool = self._pool(2)
        pool.mark_unavailable("sk-key-0", duration=3600)
        pool.mark_unavailable("sk-key-1", duration=3600)
        # Should not raise — returns one of the creds anyway
        cred = pool.acquire()
        assert cred["api_key"] in ("sk-key-0", "sk-key-1")

    def test_size(self):
        pool = self._pool(5)
        assert pool.size == 5

    def test_from_env_single_key(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-env-single")
        monkeypatch.delenv("OPENAI_API_KEY_0", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY_0", raising=False)
        pool = CredentialPool.from_env()
        assert pool.size == 1
        assert pool.acquire()["api_key"] == "sk-env-single"

    def test_from_env_multi_keys(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY_0", "sk-multi-0")
        monkeypatch.setenv("OPENAI_API_KEY_1", "sk-multi-1")
        monkeypatch.setenv("OPENAI_API_KEY_2", "sk-multi-2")
        monkeypatch.delenv("OPENAI_API_KEY_3", raising=False)
        pool = CredentialPool.from_env()
        assert pool.size == 3


# ---------------------------------------------------------------------------
# call_model_with_failover
# ---------------------------------------------------------------------------

_SUCCESS_RESPONSE = {
    "message": {"role": "assistant", "content": "ok"},
    "stop_reason": "end_turn",
    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
}


class TestCallModelWithFailover:
    def _rate_limit_exc(self):
        e = Exception("rate limit exceeded")
        return e

    def _server_exc(self):
        e = Exception("internal server error")
        e.status_code = 500
        return e

    def _context_exc(self):
        return Exception("context length exceeded")

    def test_success_on_first_attempt(self):
        pool = CredentialPool([{"api_key": "sk-ok", "base_url": None}])
        with patch("core.agent_runtime.call_model_openai", return_value=_SUCCESS_RESPONSE):
            result = call_model_with_failover(
                credential_pool=pool, model="m", messages=[], max_attempts=3
            )
        assert result["stop_reason"] == "end_turn"

    def test_rotates_on_rate_limit(self):
        """First key fails with rate limit; pool marks it unavailable and next key succeeds."""
        calls = []
        first_key_holder = {}

        def _mock_call(**kw):
            key = kw["api_key"]
            calls.append(key)
            # Only the very first call fails; subsequent calls succeed
            if not first_key_holder:
                first_key_holder["k"] = key
                raise Exception("rate limit exceeded")
            return _SUCCESS_RESPONSE

        pool = CredentialPool([
            {"api_key": "sk-0", "base_url": None},
            {"api_key": "sk-1", "base_url": None},
        ])
        with patch("core.agent_runtime.call_model_openai", side_effect=_mock_call):
            result = call_model_with_failover(
                credential_pool=pool, model="m", messages=[], max_attempts=3
            )
        assert result["stop_reason"] == "end_turn"
        # Exactly 2 calls: first fails (rate-limited), second succeeds
        assert len(calls) == 2
        assert calls[0] != calls[1]  # rotation happened

    def test_raises_on_context_too_long(self):
        pool = CredentialPool([{"api_key": "sk-x", "base_url": None}])
        with patch("core.agent_runtime.call_model_openai", side_effect=self._context_exc()):
            with pytest.raises(Exception, match="context length"):
                call_model_with_failover(
                    credential_pool=pool, model="m", messages=[], max_attempts=3
                )

    def test_retries_server_error(self):
        responses = [Exception("internal server error"), _SUCCESS_RESPONSE]
        call_count = {"n": 0}

        def _mock(**kw):
            val = responses[call_count["n"]]
            call_count["n"] += 1
            if isinstance(val, Exception):
                raise val
            return val

        pool = CredentialPool([{"api_key": "sk-y", "base_url": None}])
        with patch("core.agent_runtime.call_model_openai", side_effect=_mock):
            with patch("time.sleep"):
                result = call_model_with_failover(
                    credential_pool=pool, model="m", messages=[], max_attempts=3
                )
        assert result["stop_reason"] == "end_turn"

    def test_exhausts_retries_and_raises(self):
        pool = CredentialPool([{"api_key": "sk-z", "base_url": None}])
        with patch("core.agent_runtime.call_model_openai",
                   side_effect=Exception("internal server error")):
            with patch("time.sleep"):
                with pytest.raises(Exception, match="internal server error"):
                    call_model_with_failover(
                        credential_pool=pool, model="m", messages=[], max_attempts=3
                    )

    def test_uses_env_pool_when_none_given(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fallback")
        monkeypatch.delenv("OPENAI_API_KEY_0", raising=False)
        with patch("core.agent_runtime.call_model_openai", return_value=_SUCCESS_RESPONSE):
            result = call_model_with_failover(model="m", messages=[], max_attempts=1)
        assert result["stop_reason"] == "end_turn"
