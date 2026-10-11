"""Password hashing (bcrypt) and JWT utilities.

Deliberately has NO side-effects at import time (no settings read, no DB
access) so it can be unit-tested in isolation.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import bcrypt
import jwt


# ── password ──────────────────────────────────────────────────────────────────

def hash_password(plain_password: str) -> str:
    """Return a bcrypt hash of *plain_password*."""
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(plain_password.encode("utf-8"), salt)
    return hashed.decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Return True if *plain_password* matches *hashed_password*."""
    return bcrypt.checkpw(
        plain_password.encode("utf-8"),
        hashed_password.encode("utf-8"),
    )


# ── JWT ───────────────────────────────────────────────────────────────────────

def create_access_token(user_id: int, secret: str, algorithm: str, expire_minutes: int) -> str:
    """Create a signed JWT with sub=str(user_id) and an expiry claim."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "iat": now,
        "exp": now + timedelta(minutes=expire_minutes),
    }
    return jwt.encode(payload, secret, algorithm=algorithm)


def decode_access_token(token: str, secret: str, algorithm: str) -> int:
    """Decode *token* and return the user_id (int).

    Raises ``jwt.PyJWTError`` (including ``jwt.ExpiredSignatureError``) on any
    failure — callers are responsible for converting that to an HTTP 401.
    """
    payload = jwt.decode(token, secret, algorithms=[algorithm])
    sub = payload.get("sub")
    if sub is None:
        raise jwt.InvalidTokenError("Token is missing 'sub' claim.")
    return int(sub)
