"""FastAPI dependency: get_current_user.

Reads the Bearer token from the Authorization header, decodes it, and
returns the matching User row.  Returns HTTP 401 for every failure case
(missing token, invalid token, expired token, deleted user) without
differentiating between them to avoid leaking information.
"""

from __future__ import annotations

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.auth.security import decode_access_token
from app.config import settings
from app.dependencies import get_db
from app.models.user import User

_bearer = HTTPBearer(auto_error=False)

_CREDENTIALS_EXCEPTION = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials.",
    headers={"WWW-Authenticate": "Bearer"},
)


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User:
    """Resolve the Bearer token to a User row, or raise HTTP 401."""
    if credentials is None:
        raise _CREDENTIALS_EXCEPTION

    try:
        user_id = decode_access_token(
            credentials.credentials,
            secret=settings.jwt_secret_key,
            algorithm=settings.jwt_algorithm,
        )
    except jwt.PyJWTError:
        raise _CREDENTIALS_EXCEPTION

    user = db.get(User, user_id)
    if user is None:
        raise _CREDENTIALS_EXCEPTION

    return user


# ── role guards ───────────────────────────────────────────────────────────────

def require_roles(*allowed: str):
    """Return a FastAPI dependency that enforces one of the *allowed* role names.

    Resolution order:
      1. get_current_user runs first — 401 if token is missing/invalid/expired.
      2. The user's role name is looked up in the DB.
      3. 403 (in the app's standard error shape) if the role is not in *allowed*.

    Roles are matched exactly.  ``admin`` does NOT implicitly pass an
    ``officer``-only guard; every allowed role must be listed explicitly.
    """
    from app.models.role import Role  # local import to avoid circular import

    def _guard(
        current_user: User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> User:
        role_row = db.get(Role, current_user.role_id)
        role_name = role_row.name if role_row else ""

        if role_name not in allowed:
            from fastapi.responses import JSONResponse  # local import

            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error": {
                        "code": status.HTTP_403_FORBIDDEN,
                        "message": "You do not have permission to perform this action.",
                    }
                },
            )

        return current_user

    return _guard


# ── convenience shortcuts ─────────────────────────────────────────────────────

#: Allows only citizen
require_citizen = require_roles("citizen")

#: Allows only officer
require_officer = require_roles("officer")

#: Allows only admin
require_admin = require_roles("admin")

#: Allows officer, dept_head, or admin (any staff role)
require_staff = require_roles("officer", "dept_head", "admin")
