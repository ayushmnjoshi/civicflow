"""Audit logging service.

Public API
----------
record(db, *, user_id, action, entity_type, entity_id, details=None)
    Add an AuditLog row to *db* WITHOUT committing.  The caller's transaction
    owns the row — it succeeds or fails together with the surrounding action.

record_standalone(db, *, user_id, action, entity_type, entity_id, details=None)
    For READ events only.  Opens its own short transaction; any error is logged
    (ids/reason only, no PII) and swallowed so a failed audit write never blocks
    a view or a login.

Details sanitizer (defense in depth)
-------------------------------------
- Drops any key whose name contains: password, token, secret, email, full_name,
  phone, message, remarks, extracted_text, authorization  (case-insensitive).
- Keeps only JSON-serializable scalar/container values.
- Caps the serialized size at 2 KB; replaces an oversize value with
  {"truncated": true}.

Append-only: this module exposes NO function that updates or deletes rows.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog

logger = logging.getLogger(__name__)

# ── PII key filter ─────────────────────────────────────────────────────────────

_FORBIDDEN_KEYS: frozenset[str] = frozenset({
    "password", "token", "secret", "email", "full_name", "phone",
    "message", "remarks", "extracted_text", "authorization",
})

_MAX_DETAILS_BYTES = 2048  # 2 KB


def _is_forbidden_key(key: str) -> bool:
    k = key.lower()
    return any(forbidden in k for forbidden in _FORBIDDEN_KEYS)


def _is_json_serializable(value: Any) -> bool:
    try:
        json.dumps(value)
        return True
    except (TypeError, ValueError):
        return False


def sanitize_details(details: dict | None) -> dict | None:
    """Return a sanitized copy of *details* safe to persist.

    - Drops forbidden keys.
    - Drops non-JSON-serializable values.
    - Caps the serialized size at 2 KB.
    Returns None when *details* is None or the result is empty.
    """
    if details is None:
        return None

    clean: dict = {}
    for key, value in details.items():
        if _is_forbidden_key(str(key)):
            continue
        if not _is_json_serializable(value):
            continue
        clean[key] = value

    if not clean:
        return None

    # Size cap
    try:
        serialized = json.dumps(clean)
    except (TypeError, ValueError):
        return {"truncated": True}

    if len(serialized.encode()) > _MAX_DETAILS_BYTES:
        return {"truncated": True}

    return clean


# ── Core write helpers ─────────────────────────────────────────────────────────

def record(
    db: Session,
    *,
    user_id: int,
    action: str,
    entity_type: str,
    entity_id: int,
    details: dict | None = None,
) -> AuditLog:
    """Add an AuditLog row to the session WITHOUT committing.

    The caller's transaction owns the row.  The audit row and the surrounding
    action succeed or fail together.
    """
    row = AuditLog(
        user_id=user_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        details=sanitize_details(details),
    )
    db.add(row)
    return row


def record_standalone(
    db: Session,
    *,
    user_id: int,
    action: str,
    entity_type: str,
    entity_id: int,
    details: dict | None = None,
) -> None:
    """Write one AuditLog row in its own short transaction.

    For READ events only.  Any error is logged (ids + reason only, no PII)
    and swallowed so a failed audit write never blocks a view or a login.
    """
    try:
        row = AuditLog(
            user_id=user_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            details=sanitize_details(details),
        )
        db.add(row)
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        logger.exception(
            "audit_standalone_failed action=%s entity_type=%s entity_id=%s user_id=%s",
            action, entity_type, entity_id, user_id,
        )
