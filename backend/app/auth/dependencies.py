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
