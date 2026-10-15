"""Pydantic schemas for the workflow internal endpoints (pending-check, escalations)."""

from __future__ import annotations

from pydantic import BaseModel, Field


# ── GET /internal/applications/pending-check ──────────────────────────────────

class PendingCheckItem(BaseModel):
    """One item in GET /internal/applications/pending-check."""

    application_id: int
    status: str
    citizen_id: int
    department_id: int
    assigned_officer_user_id: int | None
    hours_in_status: int


# ── POST /internal/escalations ───────────────────────────────────────────────

class EscalationCreateRequest(BaseModel):
    """Body for POST /internal/escalations."""

    application_id: int
    reason: str = Field(min_length=1, max_length=500)


class EscalationCreateResponse(BaseModel):
    """Response for POST /internal/escalations."""

    escalation_id: int
    escalated_to_user_id: int
