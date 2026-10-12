"""Pydantic schemas for the applications endpoints."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


# ── Status history ────────────────────────────────────────────────────────────

class StatusHistoryEntry(BaseModel):
    """One entry in an application's transition log."""

    from_status: str | None
    to_status: str
    changed_by: int          # user id; never None in DB (changed_by NOT NULL)
    changed_at: datetime
    remarks: str | None


# ── Application responses ─────────────────────────────────────────────────────

class ApplicationResponse(BaseModel):
    """Flat application summary (list view)."""

    id: int
    citizen_id: int
    department_id: int
    service_type: str
    status: str
    priority: str            # label: "LOW" | "MEDIUM" | "HIGH"
    created_at: datetime
    updated_at: datetime


class ApplicationDetailResponse(ApplicationResponse):
    """Application with full status history (detail view)."""

    status_history: list[StatusHistoryEntry]


# ── Request schemas ───────────────────────────────────────────────────────────

class ApplicationCreateRequest(BaseModel):
    """POST /applications body.

    citizen_id is always taken from the token — any client-supplied value
    is rejected by the schema (no such field).
    """

    service_type: str = Field(min_length=1)
    department_id: int


class StatusUpdateRequest(BaseModel):
    """PATCH /applications/{id}/status body."""

    new_status: str = Field(min_length=1)
    remarks: str | None = None
