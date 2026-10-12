"""System actor helper.

Provides get_system_user_id(db) which returns the id of the seeded
system@civicflow.internal user.  Fails loudly if the user is absent.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_SYSTEM_EMAIL = "system@civicflow.internal"


def get_system_user_id(db: Session) -> int:
    """Return the system user's id.  Raises RuntimeError if not seeded."""
    from app.models.user import User  # local import to avoid circular dep

    user = db.query(User).filter(User.email == _SYSTEM_EMAIL).first()
    if user is None:
        raise RuntimeError(
            f"System user '{_SYSTEM_EMAIL}' not found. "
            "Run database/seed/seed.py before starting the server."
        )
    return user.id
