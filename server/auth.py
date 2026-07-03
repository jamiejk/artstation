"""Authentication and authorization helpers.

Owns token validation, localhost enforcement, and the token-requirement
check at startup.  The expected token is configured once via ``configure``
so that route handlers can call ``check_token`` without passing the
secret around explicitly.
"""

from __future__ import annotations

import os
import secrets
from typing import Optional

from fastapi import HTTPException, Request


def required_plotter_token(env: dict | None = None) -> str:
    """Read and validate PLOTTER_TOKEN from the environment."""
    env = os.environ if env is None else env
    token = env.get("PLOTTER_TOKEN", "").strip()
    if not token:
        raise RuntimeError("PLOTTER_TOKEN must be set in the service environment")
    return token


_token: str = ""


def configure(token: str) -> None:
    """Set the expected token used by ``check_token``."""
    global _token
    _token = token


def check_token(x_plotter_token: Optional[str]) -> None:
    """Reject requests that do not carry the correct X-Plotter-Token header."""
    if _token and not secrets.compare_digest(x_plotter_token or "", _token):
        raise HTTPException(status_code=401, detail="Bad or missing X-Plotter-Token")


def require_localhost(request: Request) -> None:
    """Reject requests from non-loopback clients."""
    host = request.client.host if request.client else ""
    if host not in {"127.0.0.1", "::1"}:
        raise HTTPException(
            status_code=403,
            detail="Operator controls are only available from the Linux box itself.",
        )
