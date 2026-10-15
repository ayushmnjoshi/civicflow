"""Pydantic schemas for the notifications endpoints."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


# ── POST /internal/notifications/send ────────────────────────────────────────

class NotificationSendRequest(BaseModel):
    """Body for POST /internal/notifications/send."""

    recipient_id: int
    application_id: int
    channel: str  # must be "email"; validated in the router
    message: str = Field(min_length=1, max_length=1000)


class NotificationSendResponse(BaseModel):
    """Response for POST /internal/notifications/send."""

    notification_id: int
    status: str  # "sent"


# ── GET /notifications ────────────────────────────────────────────────────────

class NotificationItem(BaseModel):
    """One item in GET /notifications."""

    id: int
    application_id: int
    channel: str
    message: str
    status: str
    sent_at: datetime | None
