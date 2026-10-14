"""Validation service — orchestrates OCR, rule-based validation, and status transitions.

Public API
----------
process_document(db, document, storage, ocr)
    Reads file bytes, runs OCR, runs validate_document, upserts ai_analysis row,
    updates documents.validation_status. Failures set ERROR; never raises to caller.

advance_application(db, application_id)
    Only acts when application is in DOCUMENT_PENDING.
    Locks the row, checks if all required docs are present, transitions
    DOCUMENT_PENDING -> VALIDATING -> VALIDATED/ADDITIONAL_DOCUMENT_REQUIRED.
    Every step uses the system user + the existing transition function so history
    rows are written correctly. Reason codes (never document content) go in remarks.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app.ai.rules import get_required_doc_types
from app.ai.validator import validate_application, validate_document
from app.constants import (
    VALIDATION_STATUS_ERROR,
    VALIDATION_STATUS_INVALID,
    VALIDATION_STATUS_PENDING,
    VALIDATION_STATUS_VALID,
)
from app.models.ai_analysis import AIAnalysis
from app.models.application import Application, ApplicationStatus
from app.models.document import Document
from app.models.user import User
from app.ocr.engine import AbstractOcrEngine, OcrError
from app.services.state_machine import validate_transition
from app.services.storage_service import AbstractStorage, StorageError
from app.services.system_actor import get_system_user_id

logger = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _upsert_ai_analysis(
    db: Session,
    document_id: int,
    extracted_text: str,
    extracted_fields: dict,
    validation_result: dict,
) -> AIAnalysis:
    """Insert or update the single ai_analysis row for *document_id*."""
    existing = (
        db.query(AIAnalysis)
        .filter(AIAnalysis.document_id == document_id)
        .first()
    )
    if existing is not None:
        existing.extracted_text = extracted_text
        existing.extracted_fields = extracted_fields
        existing.validation_result = validation_result
        existing.processed_at = _now()
        db.flush()
        return existing

    row = AIAnalysis(
        document_id=document_id,
        extracted_text=extracted_text,
        extracted_fields=extracted_fields,
        validation_result=validation_result,
        processed_at=_now(),
    )
    db.add(row)
    db.flush()
    return row


def _get_applicant_name(db: Session, application_id: int) -> str:
    """Return the citizen's full_name for the application.  Falls back to ''."""
    row = db.execute(
        text(
            "SELECT u.full_name FROM applications a "
            "JOIN users u ON u.id = a.citizen_id "
            "WHERE a.id = :app_id"
        ),
        {"app_id": application_id},
    ).fetchone()
    return row[0] if row else ""


def _transition(
    db: Session,
    app: Application,
    new_status: str,
    system_user_id: int,
    remarks: str | None = None,
) -> None:
    """Apply a system-actor transition and write a history row (no commit)."""
    from app.models.application_status_history import ApplicationStatusHistory

    old = app.status.value
    validate_transition(old, new_status, actor="system", remarks=remarks)
    app.status = ApplicationStatus(new_status)
    app.updated_at = func.now()

    db.add(ApplicationStatusHistory(
        application_id=app.id,
        from_status=old,
        to_status=new_status,
        changed_by=system_user_id,
        remarks=remarks,
    ))


# ── process_document ──────────────────────────────────────────────────────────

def process_document(
    db: Session,
    document: Document,
    storage: AbstractStorage,
    ocr: AbstractOcrEngine,
) -> None:
    """OCR and validate one document; upsert ai_analysis; update validation_status.

    This function NEVER raises: any failure is recorded as ERROR so the
    upload endpoint always returns 201.  Never logs OCR text or ID numbers.
    """
    doc_id = document.id
    doc_type = document.doc_type

    # ── 1. Read file bytes from storage ──────────────────────────────────────
    try:
        chunks = list(storage.get_stream(document.storage_ref))
        file_bytes = b"".join(chunks)
    except StorageError as exc:
        logger.error(
            "process_document: storage read failed doc_id=%d reason=storage_error",
            doc_id,
        )
        _record_error(db, document, "storage_read_error", {"reason": "storage_read_error"})
        return

    ext = document.storage_ref.rsplit(".", 1)[-1].lower() if "." in document.storage_ref else ""

    # ── 2. Run OCR ────────────────────────────────────────────────────────────
    try:
        ocr_result = ocr.extract(file_bytes, ext)
        extracted_text = ocr_result.text
        method = ocr_result.method
    except OcrError as exc:
        logger.error(
            "process_document: OCR failed doc_id=%d reason=%s",
            doc_id, type(exc).__name__,
        )
        _record_error(db, document, "ocr_error", {"reason": type(exc).__name__})
        return
    except Exception as exc:
        logger.exception(
            "process_document: unexpected OCR error doc_id=%d", doc_id
        )
        _record_error(db, document, "ocr_unexpected_error", {"reason": "ocr_unexpected_error"})
        return

    # ── 3. Run rule-based validation ──────────────────────────────────────────
    applicant_name = _get_applicant_name(db, document.application_id)
    try:
        val_result = validate_document(doc_type, extracted_text, applicant_name)
    except Exception as exc:
        logger.exception(
            "process_document: validation error doc_id=%d", doc_id
        )
        _record_error(db, document, "validation_error", {"reason": "validation_error"})
        return

    # ── 4. Upsert ai_analysis row ─────────────────────────────────────────────
    validation_status = (
        VALIDATION_STATUS_VALID if val_result["result"] == "VALID"
        else VALIDATION_STATUS_INVALID
    )

    # Store method in validation_result dict (not in extracted_text)
    result_payload = {
        "result": val_result["result"],
        "checks": val_result["checks"],
        "reasons": val_result["reasons"],
        "ocr_method": method,
    }

    try:
        _upsert_ai_analysis(
            db,
            document_id=doc_id,
            extracted_text=extracted_text,       # stored but never logged
            extracted_fields=val_result["extracted_fields"],
            validation_result=result_payload,
        )
        document.validation_status = validation_status
        db.commit()
    except Exception as exc:
        logger.exception(
            "process_document: DB write failed doc_id=%d", doc_id
        )
        db.rollback()


def _record_error(
    db: Session,
    document: Document,
    reason_code: str,
    result_payload: dict,
) -> None:
    """Upsert an error ai_analysis row and set document status=ERROR."""
    try:
        _upsert_ai_analysis(
            db,
            document_id=document.id,
            extracted_text="",
            extracted_fields={},
            validation_result={"result": "ERROR", "reason": reason_code},
        )
        document.validation_status = VALIDATION_STATUS_ERROR
        db.commit()
    except Exception:
        logger.exception(
            "process_document: could not record error row doc_id=%d", document.id
        )
        db.rollback()


# ── advance_application ───────────────────────────────────────────────────────

def advance_application(db: Session, application_id: int) -> None:
    """Transition DOCUMENT_PENDING -> VALIDATING -> VALIDATED/ADR if possible.

    - Only acts when status == DOCUMENT_PENDING.
    - Uses SELECT FOR UPDATE to prevent concurrent race conditions.
    - Uses system user as actor for every automatic transition.
    - Writes short reason codes (never doc content) into remarks.
    - Never raises to caller.
    """
    try:
        _do_advance(db, application_id)
    except Exception:
        logger.exception(
            "advance_application: unexpected error application_id=%d", application_id
        )
        try:
            db.rollback()
        except Exception:
            pass


def _do_advance(db: Session, application_id: int) -> None:
    """Internal implementation — may raise; caller handles rollback."""
    # Lock the application row
    app = (
        db.query(Application)
        .filter(Application.id == application_id)
        .with_for_update()
        .first()
    )
    if app is None:
        return
    if app.status != ApplicationStatus.DOCUMENT_PENDING:
        return

    system_user_id = get_system_user_id(db)

    # Collect documents for this application
    docs = (
        db.query(Document)
        .filter(Document.application_id == application_id)
        .all()
    )

    # Build a dict: doc_type -> list of uploaded documents
    by_type: dict[str, list[Document]] = {}
    for doc in docs:
        dt = doc.doc_type.strip().upper()
        by_type.setdefault(dt, []).append(doc)

    required = [r.strip().upper() for r in get_required_doc_types(app.service_type)]

    # Check that every required type has at least one uploaded document
    if not all(r in by_type for r in required):
        return  # not all required docs are present yet

    # ── DOCUMENT_PENDING → VALIDATING ─────────────────────────────────────────
    _transition(db, app, "VALIDATING", system_user_id)
    db.flush()

    # Build the list of doc dicts for validate_application (latest by id)
    doc_dicts = [
        {
            "id": doc.id,
            "doc_type": doc.doc_type.strip().upper(),
            "validation_status": doc.validation_status,
        }
        for doc in docs
    ]

    val = validate_application(required, doc_dicts)

    # ── VALIDATING → VALIDATED or ADDITIONAL_DOCUMENT_REQUIRED ───────────────
    if val["result"] == "VALIDATED":
        _transition(db, app, "VALIDATED", system_user_id)
    else:
        parts: list[str] = []
        if val["missing_docs"]:
            parts.append("missing:" + ",".join(val["missing_docs"]))
        if val["invalid_docs"]:
            parts.append("invalid:" + ",".join(val["invalid_docs"]))
        remarks = "; ".join(parts) or "validation_failed"
        _transition(db, app, "ADDITIONAL_DOCUMENT_REQUIRED", system_user_id, remarks)

    db.commit()
