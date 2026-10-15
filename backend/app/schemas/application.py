"""Pydantic schemas for the applications endpoints."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field, field_validator

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
    is_duplicate_flag: bool | None
    created_at: datetime
    updated_at: datetime


class ApplicationDetailResponse(ApplicationResponse):
    """Application with full status history and document info (detail view)."""

    applicant_name: str | None   # null for citizen viewers; full_name for staff
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
    urgent: bool = False


class StatusUpdateRequest(BaseModel):
    """PATCH /applications/{id}/status body."""

    new_status: str = Field(min_length=1)
    remarks: str | None = None


# ── Officer queue ─────────────────────────────────────────────────────────────

class OfficerQueueItem(BaseModel):
    """One item in GET /officer/queue."""

    id: int
    service_type: str
    status: str
    priority: str            # label: "LOW" | "MEDIUM" | "HIGH"
    is_duplicate_flag: bool | None
    applicant_name: str | None
    created_at: datetime


# ── Decision ─────────────────────────────────────────────────────────────────

_DOC_TYPE_RE = re.compile(r"^[A-Z0-9_]{1,50}$")


class DecisionRequest(BaseModel):
    """POST /applications/{id}/decision body."""

    decision: str = Field(description="approve or reject")
    remarks: str = Field(min_length=1, max_length=1000)

    @field_validator("decision")
    @classmethod
    def validate_decision(cls, v: str) -> str:
        if v not in ("approve", "reject"):
            raise ValueError("decision must be 'approve' or 'reject'")
        return v


class DecisionResponse(BaseModel):
    """POST /applications/{id}/decision response."""

    id: int
    status: str


# ── Request documents ─────────────────────────────────────────────────────────

class RequestDocumentsRequest(BaseModel):
    """POST /applications/{id}/request-documents body."""

    doc_types: Annotated[list[str], Field(min_length=1, max_length=10)]
    reason: str = Field(min_length=1, max_length=1000)

    @field_validator("doc_types")
    @classmethod
    def validate_doc_types(cls, v: list[str]) -> list[str]:
        seen: set[str] = set()
        for dt in v:
            if not _DOC_TYPE_RE.match(dt):
                raise ValueError(
                    f"Invalid doc_type '{dt}': must be uppercase letters, digits, "
                    "or underscores, max 50 chars."
                )
            if dt in seen:
                raise ValueError(f"Duplicate doc_type '{dt}'")
            seen.add(dt)
        return v


class RequestDocumentsResponse(BaseModel):
    """POST /applications/{id}/request-documents response."""

    id: int
    status: str
