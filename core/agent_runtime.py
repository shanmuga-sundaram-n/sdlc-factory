"""Thin OpenAI-compatible model call — separated so tests can patch it easily.

Uses a lazy SDK import to avoid ~240ms cold-start cost. In production, plug in
the real openai SDK. In tests, patch call_model_openai with a fixture.
"""

from __future__ import annotations

import json
import logging
import os
import time
import threading
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Lazy import — ~240ms savings on cold start
_openai_mod: Any = None
_openai_lock = threading.Lock()
_anthropic_mod: Any = None
_anthropic_lock = threading.Lock()


def _get_openai():
    global _openai_mod
    if _openai_mod is None:
        with _openai_lock:
            if _openai_mod is None:
                import openai as _sdk
                _openai_mod = _sdk
    return _openai_mod


def _get_anthropic():
    global _anthropic_mod
    if _anthropic_mod is None:
        with _anthropic_lock:
            if _anthropic_mod is None:
                import anthropic as _sdk
                _anthropic_mod = _sdk
    return _anthropic_mod


def _is_anthropic_key(key: str) -> bool:
    return key.startswith("sk-ant-")


def _convert_messages_for_anthropic(messages: List[Dict]) -> List[Dict]:
    """Convert pipeline-internal message format to Anthropic Messages API format.

    Pipeline internal format:
      - assistant with tool_calls: {"role":"assistant","content":"...","tool_calls":[...openai format...]}
      - tool results:              {"role":"tool_results","content":[{"tool_call_id":"...","content":"..."}]}

    Anthropic format:
      - assistant with tool use:   {"role":"assistant","content":[{"type":"text","text":"..."},
                                     {"type":"tool_use","id":"...","name":"...","input":{...}}]}
      - tool results:              {"role":"user","content":[{"type":"tool_result",
                                     "tool_use_id":"...","content":"..."}]}
    """
    converted = []
    for msg in messages:
        role = msg.get("role", "")

        if role == "tool_results":
            # Convert tool result batch → Anthropic user message with tool_result content blocks
            raw_results = msg.get("content", [])
            content_blocks = []
            for r in raw_results:
                content_blocks.append({
                    "type": "tool_result",
                    "tool_use_id": r.get("tool_call_id", r.get("id", "")),
                    "content": r.get("content", ""),
                })
            converted.append({"role": "user", "content": content_blocks})

        elif role == "assistant" and msg.get("tool_calls"):
            # Convert assistant message with tool calls → Anthropic content block list
            content_blocks = []
            text = msg.get("content", "")
            if text:
                content_blocks.append({"type": "text", "text": text})
            for tc in msg["tool_calls"]:
                fn = tc.get("function", {})
                raw_args = fn.get("arguments", "{}")
                try:
                    input_data = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                except (json.JSONDecodeError, TypeError):
                    input_data = {}
                content_blocks.append({
                    "type": "tool_use",
                    "id": tc.get("id", ""),
                    "name": fn.get("name", ""),
                    "input": input_data,
                })
            converted.append({"role": "assistant", "content": content_blocks})

        else:
            converted.append(msg)

    return converted


def _call_via_anthropic_sdk(
    model: str,
    messages: List[Dict],
    *,
    system: str = "",
    tools: Optional[List[Dict]] = None,
    api_key: str,
    max_tokens: int = 16000,
    temperature: float = 0.2,
    timeout: float = 600.0,
) -> Dict[str, Any]:
    """Call the Anthropic Messages API using streaming to avoid read timeouts.

    Long responses (e.g. Business Analyst BRS) can exceed 120s; streaming
    delivers tokens incrementally so no single read can time out.
    """
    sdk = _get_anthropic()
    # Use a generous connect timeout but no read timeout — streaming handles that
    http_timeout = sdk.Timeout(connect=30.0, read=timeout, write=30.0, pool=30.0)
    client = sdk.Anthropic(api_key=api_key, timeout=http_timeout)

    # Convert tools from OpenAI format to Anthropic format
    anthropic_tools = []
    if tools:
        for t in tools:
            fn = t.get("function", {})
            anthropic_tools.append({
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
            })

    # Convert messages from pipeline-internal (OpenAI-style) format to Anthropic format
    messages = _convert_messages_for_anthropic(messages)

    call_kwargs: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
    }
    if system:
        call_kwargs["system"] = system
    if anthropic_tools:
        call_kwargs["tools"] = anthropic_tools

    t0 = time.monotonic()

    # Use streaming — avoids read timeouts on long generations
    content_text = ""
    tool_calls = []
    stop_reason = "end_turn"
    input_tokens = 0
    output_tokens = 0

    with client.messages.stream(**call_kwargs) as stream:
        for event in stream:
            pass  # consume the stream; final_message collects everything
        final = stream.get_final_message()

    elapsed = time.monotonic() - t0

    for block in final.content:
        if block.type == "text":
            content_text += block.text
        elif block.type == "tool_use":
            tool_calls.append({
                "id": block.id,
                "type": "function",
                "function": {
                    "name": block.name,
                    "arguments": json.dumps(block.input),
                },
            })

    stop_reason = final.stop_reason or "end_turn"
    input_tokens  = final.usage.input_tokens
    output_tokens = final.usage.output_tokens

    assistant_msg: Dict[str, Any] = {"role": "assistant", "content": content_text}
    if tool_calls:
        assistant_msg["tool_calls"] = tool_calls

    usage = {"prompt_tokens": input_tokens, "completion_tokens": output_tokens}

    logger.debug(
        "_call_via_anthropic_sdk: stop=%s tools=%d tokens=%s elapsed=%.2fs",
        stop_reason, len(tool_calls), usage, elapsed,
    )

    return {"message": assistant_msg, "stop_reason": stop_reason, "usage": usage}


def call_model_openai(
    model: str,
    messages: List[Dict],
    *,
    system: str = "",
    tools: Optional[List[Dict]] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    max_tokens: int = 16000,
    temperature: float = 0.2,
    timeout: float = 120.0,
) -> Dict[str, Any]:
    """Make a single model call and return a normalised response dict.

    Automatically routes to the Anthropic SDK when ANTHROPIC_API_KEY is set
    and no OpenAI-compatible base URL is configured.

    Returns:
        {
          "message": {"role": "assistant", "content": ..., "tool_calls": [...]},
          "stop_reason": "end_turn" | "tool_use" | "max_tokens",
          "usage": {"prompt_tokens": int, "completion_tokens": int},
        }

    Raises on API errors (caller handles retries / failover).
    """
    resolved_key = api_key or os.getenv("OPENAI_API_KEY") or os.getenv("ANTHROPIC_API_KEY", "")
    resolved_url = base_url or os.getenv("OPENAI_BASE_URL") or os.getenv("ANTHROPIC_BASE_URL")

    # Route to Anthropic SDK when: key is an Anthropic key and no custom base URL is set
    if _is_anthropic_key(resolved_key) and not resolved_url:
        logger.debug("call_model_openai: routing to Anthropic SDK for model=%s", model)
        return _call_via_anthropic_sdk(
            model=model,
            messages=messages,
            system=system,
            tools=tools,
            api_key=resolved_key,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=timeout,
        )

    # --- OpenAI-compatible path ---
    oai = _get_openai()
    client_kwargs: Dict[str, Any] = {"api_key": resolved_key, "timeout": timeout}
    if resolved_url:
        client_kwargs["base_url"] = resolved_url

    client = oai.OpenAI(**client_kwargs)

    # Build message list — prepend system as a system message
    full_messages = []
    if system:
        full_messages.append({"role": "system", "content": system})
    full_messages.extend(messages)

    call_kwargs: Dict[str, Any] = {
        "model": model,
        "messages": full_messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if tools:
        call_kwargs["tools"] = tools
        call_kwargs["tool_choice"] = "auto"

    logger.debug("call_model_openai: model=%s messages=%d tools=%d",
                 model, len(full_messages), len(tools or []))

    t0 = time.monotonic()
    response = client.chat.completions.create(**call_kwargs)
    elapsed = time.monotonic() - t0

    choice = response.choices[0]
    msg = choice.message

    # Normalise to dict
    tool_calls = []
    if msg.tool_calls:
        for tc in msg.tool_calls:
            tool_calls.append({
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                },
            })

    assistant_msg = {
        "role": "assistant",
        "content": msg.content or "",
    }
    if tool_calls:
        assistant_msg["tool_calls"] = tool_calls

    stop_reason = choice.finish_reason or "end_turn"
    if stop_reason == "tool_calls":
        stop_reason = "tool_use"

    usage = {}
    if response.usage:
        usage = {
            "prompt_tokens": response.usage.prompt_tokens,
            "completion_tokens": response.usage.completion_tokens,
        }

    logger.debug(
        "call_model_openai: stop=%s tools_returned=%d tokens=%s elapsed=%.2fs",
        stop_reason, len(tool_calls), usage, elapsed,
    )

    return {
        "message": assistant_msg,
        "stop_reason": stop_reason,
        "usage": usage,
    }


# ---------------------------------------------------------------------------
# Jittered backoff
# ---------------------------------------------------------------------------

def jittered_backoff(attempt: int, base: float = 1.0, cap: float = 60.0) -> float:
    import random
    delay = min(cap, base * (2 ** attempt))
    return delay * (0.5 + random.random() * 0.5)


def call_model_with_retry(
    *,
    max_attempts: int = 5,
    **kwargs,
) -> Dict[str, Any]:
    """call_model_openai with exponential-backoff retry on rate limit / 5xx."""
    for attempt in range(max_attempts):
        try:
            return call_model_openai(**kwargs)
        except Exception as exc:
            msg = str(exc).lower()
            retryable = any(x in msg for x in ("rate limit", "429", "502", "503", "timeout"))
            if retryable and attempt < max_attempts - 1:
                delay = jittered_backoff(attempt)
                logger.warning("Model call failed (%s); retry %d/%d in %.1fs",
                               exc, attempt + 1, max_attempts, delay)
                time.sleep(delay)
            else:
                raise
    raise RuntimeError("unreachable")


# ---------------------------------------------------------------------------
# Multi-provider failover
# ---------------------------------------------------------------------------

class ErrorCategory(str, Enum):
    RATE_LIMIT = "rate_limit"
    AUTH_ERROR = "auth_error"
    CONTEXT_TOO_LONG = "context_too_long"
    TIMEOUT = "timeout"
    SERVER_ERROR = "server_error"
    UNKNOWN = "unknown"


def classify_api_error(exc: Exception) -> ErrorCategory:
    """Classify an API exception into a category for failover logic."""
    msg = str(exc).lower()
    code = getattr(exc, "status_code", None) or getattr(exc, "code", None)

    if code == 429 or "rate limit" in msg or "rate_limit" in msg or "429" in msg:
        return ErrorCategory.RATE_LIMIT
    if code in (401, 403) or "unauthorized" in msg or "invalid api key" in msg or "forbidden" in msg:
        return ErrorCategory.AUTH_ERROR
    if ("context" in msg and ("too long" in msg or "length" in msg)) or "token" in msg and "exceed" in msg:
        return ErrorCategory.CONTEXT_TOO_LONG
    if "timeout" in msg or code == 408:
        return ErrorCategory.TIMEOUT
    if code in (500, 502, 503, 504) or "server error" in msg or "502" in msg or "503" in msg:
        return ErrorCategory.SERVER_ERROR
    return ErrorCategory.UNKNOWN


class CredentialPool:
    """Round-robin pool of API credentials with per-key backoff.

    Build from explicit list or call from_env() to read OPENAI_API_KEY_{N}
    environment variables.
    """

    def __init__(self, credentials: List[Dict[str, Optional[str]]]):
        if not credentials:
            raise ValueError("CredentialPool requires at least one credential")
        self._creds = list(credentials)
        self._unavailable: Dict[str, float] = {}   # api_key → unavailable_until timestamp
        self._idx = 0
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls) -> "CredentialPool":
        """Build a pool from environment variables.

        Reads OPENAI_API_KEY_0/ANTHROPIC_API_KEY_0, ..._1, ..._2, etc.
        Falls back to OPENAI_API_KEY / ANTHROPIC_API_KEY as a single entry.
        """
        creds: List[Dict[str, Optional[str]]] = []
        for i in range(10):
            key = os.getenv(f"OPENAI_API_KEY_{i}") or os.getenv(f"ANTHROPIC_API_KEY_{i}")
            if not key:
                break
            base_url = os.getenv(f"OPENAI_BASE_URL_{i}") or os.getenv(f"ANTHROPIC_BASE_URL_{i}")
            creds.append({"api_key": key, "base_url": base_url})

        if not creds:
            key = os.getenv("OPENAI_API_KEY") or os.getenv("ANTHROPIC_API_KEY", "")
            base_url = os.getenv("OPENAI_BASE_URL") or os.getenv("ANTHROPIC_BASE_URL")
            creds = [{"api_key": key, "base_url": base_url}]

        return cls(creds)

    def acquire(self) -> Dict[str, Optional[str]]:
        """Return the next available credential (round-robin, skipping unavailable)."""
        with self._lock:
            now = time.time()
            available = [c for c in self._creds
                         if c["api_key"] not in self._unavailable
                         or self._unavailable[c["api_key"]] <= now]
            if not available:
                available = self._creds  # all rate-limited; caller will sleep
            self._idx = (self._idx + 1) % len(available)
            return available[self._idx % len(available)]

    def mark_unavailable(self, api_key: str, duration: float = 60.0) -> None:
        """Mark a credential as unavailable for *duration* seconds."""
        with self._lock:
            self._unavailable[api_key] = time.time() + duration
        logger.warning(
            "Credential %s…%s marked unavailable for %.0fs",
            api_key[:8], api_key[-4:] if len(api_key) > 8 else "", duration,
        )

    @property
    def size(self) -> int:
        return len(self._creds)

    @property
    def available_count(self) -> int:
        now = time.time()
        with self._lock:
            return sum(
                1 for c in self._creds
                if c["api_key"] not in self._unavailable
                or self._unavailable[c["api_key"]] <= now
            )


def call_model_with_failover(
    *,
    credential_pool: Optional["CredentialPool"] = None,
    max_attempts: int = 5,
    **kwargs,
) -> Dict[str, Any]:
    """call_model_openai with multi-credential failover + exponential backoff.

    On RATE_LIMIT: mark the credential unavailable and try the next one.
    On AUTH_ERROR: mark unavailable for 1 hour (likely invalid key).
    On SERVER_ERROR / TIMEOUT: sleep with jitter and retry same key.
    On CONTEXT_TOO_LONG / UNKNOWN: raise immediately (not retryable).
    """
    pool = credential_pool
    if pool is None:
        # Lazy singleton pool built from env vars
        pool = CredentialPool.from_env()

    last_exc: Optional[Exception] = None
    for attempt in range(max_attempts):
        cred = pool.acquire()
        kw = dict(kwargs)
        kw["api_key"] = cred.get("api_key")
        if cred.get("base_url"):
            kw["base_url"] = cred["base_url"]

        try:
            return call_model_openai(**kw)
        except Exception as exc:
            last_exc = exc
            cat = classify_api_error(exc)
            api_key = cred.get("api_key", "")

            if cat == ErrorCategory.RATE_LIMIT:
                pool.mark_unavailable(api_key, duration=60.0)
                if pool.available_count > 0:
                    continue  # try another key immediately
                delay = jittered_backoff(attempt)
                logger.warning("All credentials rate-limited; sleeping %.1fs", delay)
                time.sleep(delay)

            elif cat == ErrorCategory.AUTH_ERROR:
                pool.mark_unavailable(api_key, duration=3600.0)
                if pool.available_count == 0:
                    raise

            elif cat in (ErrorCategory.TIMEOUT, ErrorCategory.SERVER_ERROR):
                delay = jittered_backoff(attempt)
                logger.warning(
                    "Transient error (%s) on attempt %d/%d; sleeping %.1fs",
                    cat.value, attempt + 1, max_attempts, delay,
                )
                time.sleep(delay)

            else:
                # CONTEXT_TOO_LONG, UNKNOWN — not retryable
                raise

            if attempt == max_attempts - 1:
                raise

    if last_exc is not None:
        raise last_exc
    raise RuntimeError("unreachable")
