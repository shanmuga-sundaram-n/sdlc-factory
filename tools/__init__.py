"""Auto-discover and register all built-in tool modules.

Importing this package triggers discover_tools(), which imports every
non-underscore .py file in this directory. Each module calls
registry.register() at module level, so they self-register into the
global ToolRegistry singleton.

Import this package once at pipeline startup:
    import tools  # noqa: F401  — side-effect: registers all tools
"""

from core.registry import discover_tools as _discover

_discover()
