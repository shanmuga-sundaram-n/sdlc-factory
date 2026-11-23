"""Command guardrail for the agent terminal tool.

This is an accident guardrail, NOT a security boundary. It stops an agent from
running the obvious dangerous spellings (a shell, ``curl``, ``rm``, ``python -c``,
``cat /etc/hosts``, ``cp x ../../y``). It does not sandbox anything: allowed tools
such as ``make``, ``npm run``, ``pip install``, ``git``, ``pytest`` and
``python script.py`` still execute arbitrary code on the host, and only the first
word of a command is matched, so wrappers like ``uv run``, ``npx``, ``find -exec``
and ``git -c`` reach further than their name suggests. See SECURITY.md. The real
isolation is a container sandbox, which is future work.

What it enforces, in order:

1. The command name (basename, so ``/bin/bash`` and ``./sh`` do not slip past)
   must not be on the hard deny list, and must be on the allowlist.
2. ``python -c`` and ``node -e/-p/--eval/--print`` run inline code and are
   refused; write a script file instead.
3. No argument, nor the value after ``--flag=``, may be an absolute path, start
   with ``~``, or contain a ``..`` path component, and a symlink inside the
   workspace may not lead out of it.

The allowlist is the built-in default plus an optional override file,
``~/.sdlc-factory/terminal_allowlist.yaml``, which lives outside every agent
workspace and is read once per process::

    allow: [docker]     # extra command names to permit
    remove: [cp]        # default command names to take away

A missing file means the defaults. An unreadable or invalid file fails closed:
every command is refused with a message naming the file. Names on the deny list
can never be allowed, not even through this file.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import FrozenSet, Iterator, Optional, Sequence

import yaml

from tools._context import safe_resolve

logger = logging.getLogger(__name__)

DEFAULT_ALLOWED: FrozenSet[str] = frozenset(
    {
        # language runtimes, package managers and build tools
        "python",
        "python3",
        "pytest",
        "uv",
        "pip",
        "pip3",
        "npm",
        "npx",
        "node",
        "pnpm",
        "yarn",
        "git",
        "go",
        "cargo",
        "make",
        "ruff",
        "black",
        # everyday file and text utilities
        "ls",
        "cat",
        "echo",
        "pwd",
        "mkdir",
        "touch",
        "cp",
        "mv",
        "grep",
        "find",
        "head",
        "tail",
        "wc",
        "diff",
        "sort",
        "true",
        "false",
        "sleep",
    }
)

# A hard floor: refused even if the override file asks for them.
DENIED: FrozenSet[str] = frozenset(
    {"sh", "bash", "zsh", "curl", "wget", "ssh", "nc", "rm", "sudo", "env"}
)

POLICY_FILE_NAME = "terminal_allowlist.yaml"
_NAME_RE = re.compile(r"^[A-Za-z0-9._+-]+$")
_PYTHON_VERSIONED_RE = re.compile(r"^python3\.\d+$")


def policy_path() -> Path:
    return Path.home() / ".sdlc-factory" / POLICY_FILE_NAME


@dataclass(frozen=True)
class Policy:
    allowed: FrozenSet[str]
    error: Optional[str] = None  # set when the override file is unusable: refuse everything
    source: Optional[Path] = None


def _invalid(path: Path, why: str) -> Policy:
    message = (
        f"Terminal policy file {path} is invalid: {why}. "
        "Fix or delete it; every terminal command is refused until then."
    )
    logger.error(message)
    return Policy(allowed=frozenset(), error=message, source=path)


def load_policy(path: Optional[Path] = None) -> Policy:
    """Build the policy from the defaults and the optional override file."""
    path = path or policy_path()
    if not path.exists():
        return Policy(allowed=DEFAULT_ALLOWED)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        return _invalid(path, f"cannot read it ({exc})")
    if raw is None:
        return Policy(allowed=DEFAULT_ALLOWED, source=path)
    if not isinstance(raw, dict):
        return _invalid(path, "expected a mapping with 'allow' and/or 'remove' lists")
    unknown = sorted(set(raw) - {"allow", "remove"})
    if unknown:
        return _invalid(path, f"unknown key(s) {unknown}; only 'allow' and 'remove' are used")

    lists = {}
    for key in ("allow", "remove"):
        value = raw.get(key) or []
        if not isinstance(value, list) or not all(
            isinstance(v, str) and _NAME_RE.match(v) for v in value
        ):
            return _invalid(path, f"'{key}' must be a list of plain command names")
        lists[key] = set(value)

    forbidden = sorted(lists["allow"] & DENIED)
    if forbidden:
        return _invalid(
            path, f"{forbidden} can never be allowed (shells, network tools, destructive commands)"
        )
    allowed = (DEFAULT_ALLOWED | lists["allow"]) - lists["remove"]
    logger.info("Terminal policy loaded from %s (%d commands)", path, len(allowed))
    return Policy(allowed=frozenset(allowed), source=path)


_cached: Optional[Policy] = None


def get_policy() -> Policy:
    """The process-wide policy, loaded on first use and then fixed."""
    global _cached
    if _cached is None:
        _cached = load_policy()
    return _cached


def reset_policy_cache() -> None:
    """Forget the loaded policy (tests)."""
    global _cached
    _cached = None


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def _canonical(name: str) -> str:
    """Versioned interpreters (python3.12) follow the rule for python3."""
    return "python3" if _PYTHON_VERSIONED_RE.match(name) else name


def _python_inline_code(rest: Sequence[str]) -> bool:
    """True if the interpreter options before the script/module include ``-c``."""
    i = 0
    while i < len(rest):
        token = rest[i]
        if token == "--" or token == "-" or not token.startswith("-"):
            return False  # first non-option: a script path; everything after is its own
        if token.startswith("--"):
            i += 1
            continue
        letters = token[1:]
        for j, ch in enumerate(letters):
            if ch == "c":
                return True
            if ch == "m":
                return False  # -m <module>: what follows belongs to the module
            if ch in "WXQ":  # takes a value: the rest of this token, or the next one
                if j == len(letters) - 1:
                    i += 1
                break
        i += 1
    return False


_NODE_INLINE = {"-e", "--eval", "-p", "--print", "-pe", "-ep"}
_NODE_TAKES_VALUE = {"-r", "--require", "--import", "--loader", "--experimental-loader"}


def _node_inline_code(rest: Sequence[str]) -> bool:
    i = 0
    while i < len(rest):
        token = rest[i]
        if not token.startswith("-"):
            return False  # first non-option: the script path
        if token in _NODE_INLINE or token.startswith(("--eval=", "--print=")):
            return True
        if token in _NODE_TAKES_VALUE:
            i += 1
        i += 1
    return False


def _path_candidates(arg: str) -> Iterator[str]:
    if arg.startswith("-"):
        if arg.startswith("--") and "=" in arg:
            yield arg.split("=", 1)[1]  # --flag=value
        return  # a bare flag carries no path
    yield arg


def _path_problem(value: str, cwd_rel: str) -> Optional[str]:
    if not value:
        return None
    if value.startswith(("/", "~")) or ".." in value.split("/"):
        return (
            f"Argument {value!r} is an absolute, home or parent-directory path. "
            "Use paths relative to the working directory, inside the workspace."
        )
    try:
        # Resolves symlinks: a link inside the workspace must not lead out of it.
        safe_resolve(str(Path(cwd_rel) / value))
    except PermissionError:
        return f"Argument {value!r} leads outside the workspace (symlink). Keep paths inside it."
    except (ValueError, OSError):
        pass  # not a usable path at all (e.g. plain text): nothing to confine
    return None


def check(args: Sequence[str], cwd_rel: str = ".") -> Optional[str]:
    """Return None if the command may run, else the reason, written for the agent."""
    policy = get_policy()
    if policy.error:
        return policy.error
    if not args:
        return "Empty command"

    name = _canonical(os.path.basename(args[0]))
    if name in DENIED:
        return (
            f"Command {name!r} is blocked: shells, network tools and destructive commands "
            "are not allowed. Use a script file run by an allowed interpreter instead."
        )
    if name not in policy.allowed:
        shown = ", ".join(sorted(policy.allowed))
        return (
            f"Command {name!r} is not on the terminal allowlist. Allowed: {shown}. "
            f"To permit it, add it to {policy_path()}."
        )

    rest = list(args[1:])
    inline = (name in {"python", "python3"} and _python_inline_code(rest)) or (
        name == "node" and _node_inline_code(rest)
    )
    if inline:
        return (
            f"{name} with -c/-e/--eval/-p runs inline code and is not allowed. "
            "Write the code to a file and run that file."
        )

    for arg in rest:
        for value in _path_candidates(arg):
            problem = _path_problem(value, cwd_rel)
            if problem:
                return problem
    return None
