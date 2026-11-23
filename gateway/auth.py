"""Bearer-token auth for the dashboard API.

``python sdlc.py ui`` mints a random token per launch and exports it as
``SDLC_UI_TOKEN`` before starting uvicorn (uvicorn imports the app by string,
and with ``--reload`` in a child process, so an environment variable is the
one handoff that reaches the app). Every ``/api`` route depends on
``require_token``; the SPA shell at ``/`` stays public so the browser can load
the page that reads the token from the URL fragment.

The token is compared in constant time. With no token configured every
request is refused, so a missing variable can never leave the API open.
"""

from __future__ import annotations

import os
import secrets
from typing import Optional

from fastapi import Header, HTTPException

TOKEN_ENV = "SDLC_UI_TOKEN"


def require_token(authorization: Optional[str] = Header(default=None)) -> None:
    """FastAPI dependency: 401 unless ``Authorization: Bearer <token>`` matches."""
    expected = os.environ.get(TOKEN_ENV, "")
    scheme, _, supplied = (authorization or "").partition(" ")
    ok = (
        bool(expected)
        and scheme.lower() == "bearer"
        and secrets.compare_digest(supplied.strip().encode(), expected.encode())
    )
    if not ok:
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid access token",
            headers={"WWW-Authenticate": "Bearer"},
        )
