"""Per-tenant credential and configuration loader.

Tenants isolate pipeline runs in the DB and can supply their own API keys via
~/.sdlc-factory/tenants/<tenant_id>.env (KEY=VALUE format, one per line).

Usage:
    creds = TenantConfig.load("acme")
    creds.apply_to_env()   # sets os.environ for the current process
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

_TENANT_DIR = Path.home() / ".sdlc-factory" / "tenants"


class TenantConfig:
    """Credentials and overrides for a single tenant."""

    def __init__(self, tenant_id: str, credentials: Dict[str, str]):
        self.tenant_id = tenant_id
        self._creds = credentials

    @classmethod
    def load(cls, tenant_id: str, tenant_dir: Optional[Path] = None) -> "TenantConfig":
        """Load credentials from ~/.sdlc-factory/tenants/<tenant_id>.env.

        File format: KEY=VALUE lines; blank lines and lines starting with # ignored.
        Returns an empty TenantConfig if the file doesn't exist.
        """
        base = tenant_dir or _TENANT_DIR
        env_file = base / f"{tenant_id}.env"
        creds: Dict[str, str] = {}
        if env_file.exists():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, _, val = line.partition("=")
                    creds[key.strip()] = val.strip()
            logger.info(
                "Loaded %d credentials for tenant %r from %s", len(creds), tenant_id, env_file
            )
        else:
            logger.debug(
                "No tenant env file for %r (looked at %s); using process env", tenant_id, env_file
            )
        return cls(tenant_id=tenant_id, credentials=creds)

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Return tenant-specific value, falling back to os.environ."""
        return self._creds.get(key) or os.getenv(key, default)

    @property
    def api_key(self) -> Optional[str]:
        return self.get("OPENAI_API_KEY") or self.get("ANTHROPIC_API_KEY")

    @property
    def base_url(self) -> Optional[str]:
        return self.get("OPENAI_BASE_URL") or self.get("ANTHROPIC_BASE_URL")

    def apply_to_env(self) -> None:
        """Overlay tenant credentials onto os.environ (process-wide, use with care)."""
        for key, val in self._creds.items():
            os.environ[key] = val
        logger.debug(
            "Applied %d tenant credentials to environment for %r", len(self._creds), self.tenant_id
        )

    def __repr__(self) -> str:
        return f"TenantConfig(tenant_id={self.tenant_id!r}, keys={list(self._creds)})"
