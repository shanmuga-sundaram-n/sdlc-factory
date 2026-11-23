"""Dynamic custom agent role loader.

Reads YAML files from a directory (default: custom_roles/) and generates
BaseSDLCAgent subclasses on the fly.  Each YAML file must end in .yaml
(not .yaml.sample) and follow the schema in example-data-engineer.yaml.sample.

Usage (called from SDLCPipeline.__init__):
    from agents.custom_loader import load_custom_roles
    extra_roles = load_custom_roles()           # uses default custom_roles/ dir
    pipeline._agent_registry.update(extra_roles)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, Type

logger = logging.getLogger(__name__)

_CUSTOM_ROLES_DIR = Path(__file__).parent.parent / "custom_roles"


def load_custom_roles(
    roles_dir: Path | None = None,
) -> Dict[str, Type]:
    """Scan *roles_dir* for *.yaml files and return a role_key → class mapping.

    Files ending in .yaml.sample are skipped.
    Returns an empty dict (and logs a warning) if the dir doesn't exist.
    """
    target = roles_dir or _CUSTOM_ROLES_DIR
    if not target.exists():
        logger.debug("Custom roles directory %s does not exist; skipping", target)
        return {}

    loaded: Dict[str, Type] = {}
    for yaml_path in sorted(target.glob("*.yaml")):
        if yaml_path.name.endswith(".yaml.sample"):
            continue
        try:
            cls = _build_class_from_file(yaml_path)
            loaded[cls.ROLE] = cls
            logger.info("Loaded custom role %r from %s", cls.ROLE, yaml_path.name)
        except Exception as exc:
            logger.warning("Failed to load custom role from %s: %s", yaml_path, exc)

    if loaded:
        logger.info("Registered %d custom agent role(s): %s", len(loaded), list(loaded))
    return loaded


def _build_class_from_file(yaml_path: Path) -> Type:
    """Parse a YAML role definition and return a dynamic BaseSDLCAgent subclass."""
    import yaml

    with yaml_path.open(encoding="utf-8") as f:
        spec = yaml.safe_load(f)
    return _build_class(spec)


def _build_class(spec: dict) -> Type:
    """Create a BaseSDLCAgent subclass from a role spec dict.

    Required keys: role, display_name, authority_statement, output_schema
    Optional keys: skill_md_path, max_iterations, enabled_toolsets, prior_stages
    """
    from core.agent_base import BaseSDLCAgent

    role = spec.get("role", "")
    if not role:
        raise ValueError("Custom role spec missing 'role' key")

    required = ["display_name", "authority_statement", "output_schema"]
    for key in required:
        if key not in spec:
            raise ValueError(f"Custom role {role!r} missing required key {key!r}")

    prior = spec.get("prior_stages", [])

    class _DynamicAgent(BaseSDLCAgent):
        ROLE = role
        ROLE_DISPLAY = spec["display_name"]
        SKILL_MD_PATH = spec.get("skill_md_path", "")
        AUTHORITY_STATEMENT = spec["authority_statement"]
        OUTPUT_SCHEMA = spec["output_schema"]
        ENABLED_TOOLSETS = spec.get("enabled_toolsets", [])
        MAX_ITERATIONS = int(spec.get("max_iterations", 30))

        def prior_stages(self):
            return list(prior)

    _DynamicAgent.__name__ = f"CustomAgent_{role}"
    _DynamicAgent.__qualname__ = f"CustomAgent_{role}"
    return _DynamicAgent
