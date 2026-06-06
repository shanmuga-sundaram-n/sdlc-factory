"""Tool registry for the SDLC pipeline.

Self-registering tools: each tool module calls ``registry.register()``
at module level. The pipeline queries the registry by toolset name to
build the schema for each agent's role.

ToolEntry supports a TTL-cached ``check_fn`` that gates tool availability
at runtime without blocking the hot path.
"""

from __future__ import annotations

import importlib
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# check_fn TTL cache (30s) — avoids blocking the hot path on availability checks
# ---------------------------------------------------------------------------

_CHECK_FN_TTL_SECONDS = 30.0
_check_fn_cache: Dict[Callable, tuple[float, bool]] = {}
_check_fn_cache_lock = threading.Lock()


def _check_fn_cached(fn: Callable) -> bool:
    now = time.monotonic()
    with _check_fn_cache_lock:
        cached = _check_fn_cache.get(fn)
        if cached is not None:
            ts, value = cached
            if now - ts < _CHECK_FN_TTL_SECONDS:
                return value
    try:
        value = bool(fn())
    except Exception:
        value = False
    with _check_fn_cache_lock:
        _check_fn_cache[fn] = (now, value)
    return value


def invalidate_check_fn_cache() -> None:
    with _check_fn_cache_lock:
        _check_fn_cache.clear()


# ---------------------------------------------------------------------------
# ToolEntry
# ---------------------------------------------------------------------------

class ToolEntry:
    __slots__ = (
        "name", "toolset", "schema", "handler",
        "check_fn", "is_async", "description", "emoji",
    )

    def __init__(
        self,
        name: str,
        toolset: str,
        schema: dict,
        handler: Callable,
        check_fn: Optional[Callable] = None,
        is_async: bool = False,
        description: str = "",
        emoji: str = "🔧",
    ):
        self.name = name
        self.toolset = toolset
        self.schema = schema
        self.handler = handler
        self.check_fn = check_fn
        self.is_async = is_async
        self.description = description
        self.emoji = emoji


# ---------------------------------------------------------------------------
# ToolRegistry — thread-safe, generation-tracked
# ---------------------------------------------------------------------------

class ToolRegistry:
    """Singleton registry that collects tool schemas + handlers from tool files.

    A generation counter lets external callers memoize against a stable snapshot
    and invalidate cheaply when new tools are registered.
    """

    def __init__(self):
        self._tools: Dict[str, ToolEntry] = {}
        self._lock = threading.RLock()
        self._generation: int = 0

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(
        self,
        name: str,
        handler: Callable,
        schema: dict,
        toolset: str,
        check_fn: Optional[Callable] = None,
        is_async: bool = False,
        description: str = "",
        emoji: str = "🔧",
    ) -> None:
        with self._lock:
            if name in self._tools:
                logger.debug("Re-registering tool %s", name)
            self._tools[name] = ToolEntry(
                name=name,
                toolset=toolset,
                schema=schema,
                handler=handler,
                check_fn=check_fn,
                is_async=is_async,
                description=description,
                emoji=emoji,
            )
            self._generation += 1

    def deregister(self, name: str) -> bool:
        with self._lock:
            if name in self._tools:
                del self._tools[name]
                self._generation += 1
                return True
            return False

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def get_entry(self, name: str) -> Optional[ToolEntry]:
        with self._lock:
            return self._tools.get(name)

    def get_definitions(
        self,
        enabled_toolsets: Optional[List[str]] = None,
        blocked_tools: Optional[set] = None,
    ) -> List[Dict[str, Any]]:
        """Return OpenAI-style tool definitions filtered by toolset and availability.

        Parameters
        ----------
        enabled_toolsets:
            If given, only include tools whose ``toolset`` is in this list.
            None means include all toolsets.
        blocked_tools:
            Tool names that are always excluded regardless of toolset.
        """
        blocked = blocked_tools or set()
        with self._lock:
            entries = list(self._tools.values())

        result = []
        for entry in entries:
            if entry.name in blocked:
                continue
            if enabled_toolsets is not None and entry.toolset not in enabled_toolsets:
                continue
            if entry.check_fn is not None and not _check_fn_cached(entry.check_fn):
                continue
            result.append({
                "type": "function",
                "function": {
                    "name": entry.name,
                    "description": entry.description or entry.schema.get("description", ""),
                    "parameters": entry.schema.get("parameters", {}),
                },
            })
        return result

    def handle(self, name: str, arguments: dict) -> Any:
        """Dispatch a tool call by name. Raises KeyError if not found."""
        entry = self.get_entry(name)
        if entry is None:
            raise KeyError(f"Unknown tool: {name!r}")
        return entry.handler(**arguments)

    def list_toolsets(self) -> List[str]:
        with self._lock:
            return sorted({e.toolset for e in self._tools.values()})

    def list_tools_in_toolset(self, toolset: str) -> List[str]:
        with self._lock:
            return sorted(e.name for e in self._tools.values() if e.toolset == toolset)


# ---------------------------------------------------------------------------
# Module-level singleton (import this everywhere)
# ---------------------------------------------------------------------------

registry = ToolRegistry()


# ---------------------------------------------------------------------------
# Tool auto-discovery
# ---------------------------------------------------------------------------

def discover_tools(tools_dir: Optional[Path] = None) -> List[str]:
    """Import self-registering tool modules from tools_dir.

    Returns list of imported module names. Import errors are logged as
    warnings, not raised, so a broken optional tool doesn't crash startup.
    """
    tools_path = tools_dir or Path(__file__).parent.parent / "tools"
    imported = []
    for path in sorted(tools_path.glob("*.py")):
        if path.name.startswith("_"):
            continue
        mod_name = f"tools.{path.stem}"
        try:
            importlib.import_module(mod_name)
            imported.append(mod_name)
        except Exception as exc:
            logger.warning("Could not import tool module %s: %s", mod_name, exc)
    return imported


# ---------------------------------------------------------------------------
# Convenience: structured error return
# ---------------------------------------------------------------------------

def tool_error(message: str) -> str:
    import json
    return json.dumps({"error": message})
