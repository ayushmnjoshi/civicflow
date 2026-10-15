"""Pydantic schemas for the documents endpoints."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator
import re


# ── Request ───────────────────────────────────────────────────────────────────

_DOC_TYPE_RE = re.compile(r"^[A-Z0-9_]{1,50}$")


class DocumentUploadRequest(BaseModel):
    """Form data accompanying a multipart file upload.

    doc_type: stripped, uppercased, 1-50 chars, letters/digits/underscore only.
    """

    doc_type: str = Field(min_length=1, max_length=50)

    @field_validator("doc_type", mode="before")
    @classmethod
    def normalise_doc_type(cls, v: str) -> str:
        v = str(v).strip().upper()
        if not _DOC_TYPE_RE.match(v):
            raise ValueError(
                "doc_type must be 1-50 characters, letters/digits/underscore only."
            )
        return v


# ── Responses ─────────────────────────────────────────────────────────────────

class DocumentUploadResponse(BaseModel):
    """Response body for a successful document upload (201)."""

    document_id: int
    storage_ref: str


class DocumentSummary(BaseModel):
    """Document summary for embedding in application detail responses.

    Never exposes storage_ref or extracted text.
    """

    id: int
    doc_type: str
    original_filename: str
    validation_status: str
    uploaded_at: datetime

class DocumentResponse(BaseModel):
    """Flat document summary (for internal use / future list endpoint)."""

    id: int
    application_id: int
    doc_type: str
    storage_ref: str
    original_filename: str
    validation_status: str
    uploaded_at: datetime


class DocumentAnalysisResponse(BaseModel):
    """Response for GET /documents/{id}/analysis (staff-only).

    Never exposes extracted_text.
    extracted_fields already has ID numbers masked (stored masked in DB).
    """

    document_id: int
    doc_type: str
    validation_status: str
    extracted_fields: dict | None
    validation_result: dict | None
    processed_at: datetime
