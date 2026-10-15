"""Notifications router — GET /notifications.

Returns only the calling user's own notifications, newest first.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
from app.dependencies import get_db
from app.models.notification import Notification
from app.models.user import User
from app.schemas.notification import NotificationItem

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get(
    "",
    response_model=list[NotificationItem],
    summary="List the caller's notifications",
)
def list_notifications(
    limit: int = Query(default=50, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[NotificationItem]:
    """Return the caller's own notifications, newest first.

    - ``limit`` must be 1–100 (default 50); out-of-range → 422.
    - Never returns another user's notifications.
    """
    rows = (
        db.query(Notification)
        .filter(Notification.recipient_id == current_user.id)
        .order_by(Notification.id.desc())
        .limit(limit)
        .all()
    )
    return [
        NotificationItem(
            id=r.id,
            application_id=r.application_id,
            channel=r.channel,
            message=r.message,
            status=r.status,
            sent_at=r.sent_at,
        )
        for r in rows
    ]
