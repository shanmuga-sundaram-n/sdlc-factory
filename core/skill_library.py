"""Pre-built SKILL.md fragment library.

Agents can call skill_view(name) to inject architecture pattern fragments into
their context. The library lives in skills/library/ and ships with three
built-in fragments: rest-api, event-driven, microservices.

The SkillLibrary is a lightweight path resolver — no caching needed since
fragments are read at invocation time.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_LIBRARY_DIR = Path(__file__).parent.parent / "skills" / "library"


class SkillLibrary:
    """Resolver for SKILL.md library fragments."""

    def __init__(self, library_dir: Optional[Path] = None):
        self._dir = library_dir or _LIBRARY_DIR

    def list_skills(self) -> List[Dict[str, str]]:
        """Return a list of available skills: [{name, path, summary}]."""
        if not self._dir.exists():
            return []
        skills = []
        for p in sorted(self._dir.glob("*.md")):
            name = p.stem
            content = p.read_text(encoding="utf-8")
            # First non-empty non-# line as summary
            summary = ""
            for line in content.splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    summary = line[:120]
                    break
            skills.append({"name": name, "path": str(p), "summary": summary})
        return skills

    def get_skill(self, name: str) -> Optional[str]:
        """Return the full content of a skill fragment, or None if not found."""
        p = self._dir / f"{name}.md"
        if not p.exists():
            logger.warning("Skill fragment %r not found in %s", name, self._dir)
            return None
        content = p.read_text(encoding="utf-8")
        logger.debug("Skill fragment %r loaded (%d chars)", name, len(content))
        return content

    def skill_exists(self, name: str) -> bool:
        return (self._dir / f"{name}.md").exists()


# Module-level singleton
_default_library: Optional[SkillLibrary] = None


def get_skill_library(library_dir: Optional[Path] = None) -> SkillLibrary:
    global _default_library
    if _default_library is None:
        _default_library = SkillLibrary(library_dir)
    return _default_library
