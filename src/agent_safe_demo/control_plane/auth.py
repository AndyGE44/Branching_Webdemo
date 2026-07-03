"""Optional Basic Auth across the whole app, embedded shops included.

Enabled whenever ``DEMO_AUTH_PASSWORD`` is set (deploy/serve-public.sh
guarantees that for public runs). The control plane runs as root for
CRIU/podman, so it must never face the network unauthenticated.
"""

from __future__ import annotations

import base64
import os
import secrets

from fastapi import Request
from fastapi.responses import JSONResponse


def auth_enabled() -> bool:
    return bool(os.getenv("DEMO_AUTH_PASSWORD"))


def _valid_basic_auth(authorization: str | None) -> bool:
    if not authorization or not authorization.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(authorization.removeprefix("Basic ")).decode()
    except (UnicodeDecodeError, ValueError):
        return False
    username, separator, password = decoded.partition(":")
    if not separator:
        return False
    expected_user = os.getenv("DEMO_AUTH_USER", "demo")
    expected_password = os.getenv("DEMO_AUTH_PASSWORD") or ""
    return secrets.compare_digest(username, expected_user) and secrets.compare_digest(
        password, expected_password
    )


async def require_basic_auth(request: Request, call_next):
    # The liveness probe stays open so a local supervisor / uptime check can
    # reach it without credentials. It exposes nothing but {"status": "ok"}.
    if request.url.path == "/healthz":
        return await call_next(request)
    if auth_enabled() and not _valid_basic_auth(request.headers.get("authorization")):
        realm = os.getenv("DEMO_AUTH_REALM", "Agent-Safe Demo")
        return JSONResponse(
            {"detail": "Authentication required"},
            status_code=401,
            headers={"WWW-Authenticate": f'Basic realm="{realm}"'},
        )
    return await call_next(request)
