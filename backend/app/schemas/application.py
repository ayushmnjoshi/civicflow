"""Pydantic schemas for the applications endpoints."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.document import DocumentSummary


# ── Status history ────────────────────────────────────────────────────────────

class StatusHistoryEntry(BaseModel):
    """One entry in an application's transition log.

    ``changed_by`` (raw user id) is NOT exposed in the API response.
    Instead, the API returns ``actor_role`` and ``actor_name``.

    ``actor_name`` is:
      - "System"       for the system actor
      - person's full_name  for staff viewers (officer / dept_head / admin)
      - null           for a citizen viewer
    """

    from_status: str | None
    to_status: str
    actor_role: str          # "citizen" | "system" | "officer" | "dept_head" | "admin"
    actor_name: str | None   # null for a citizen viewer
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
    """Application with full status history and document info (detail view)."""

    status_history: list[StatusHistoryEntry]
    documents: list[DocumentSummary]
    required_documents: list[str]
    missing_documents: list[str]
    invalid_documents: list[str]


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
