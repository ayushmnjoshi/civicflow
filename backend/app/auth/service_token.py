"""Service-token authentication dependency for internal endpoints.

The X-Service-Token header is compared with hmac.compare_digest to prevent
timing attacks.  A missing, empty, or wrong token always returns 401 with a
generic message — callers cannot distinguish "missing token" from "wrong token".

Fail-closed: if INTERNAL_SERVICE_TOKEN is empty or still the placeholder in
.env, EVERY request is rejected with 401.

A normal user JWT must NOT satisfy this check — this dependency reads only
the X-Service-Token header.
"""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, status


_PLACEHOLDER = ""   # empty string == not configured

_DENIED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Service token required.",
)


def require_service_token(
    x_service_token: str | None = Header(default=None, alias="X-Service-Token"),
) -> None:
    """Raise HTTP 401 if X-Service-Token is missing, empty, or incorrect.

    Uses hmac.compare_digest for constant-time comparison.
    Fails closed if INTERNAL_SERVICE_TOKEN is not configured.
    """
    from app.config import settings

    configured = settings.internal_service_token

    # Fail closed: empty/unconfigured token rejects everything
    if not configured or configured == _PLACEHOLDER:
        raise _DENIED

    # Missing or empty header
    if not x_service_token:
        raise _DENIED

    # Constant-time comparison to prevent timing attacks
    if not hmac.compare_digest(
        configured.encode("utf-8"),
        x_service_token.encode("utf-8"),
    ):
        raise _DENIED
