"""Pydantic schemas for the internal AI/OCR endpoints."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


# ── Request bodies ─────────────────────────────────────────────────────────────

class OcrRequest(BaseModel):
    document_id: int


class ValidateRequest(BaseModel):
    application_id: int


# ── Response bodies ────────────────────────────────────────────────────────────

class OcrResponse(BaseModel):
    extracted_text: str
    extracted_fields: dict[str, Any]


class ValidateResponse(BaseModel):
    validation_result: dict[str, Any]
    missing_docs: list[str]
