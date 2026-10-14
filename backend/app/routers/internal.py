"""Internal service endpoints.

Protected by X-Service-Token (see app/auth/service_token.py).
All routes use include_in_schema=False so they do not appear in /docs.

POST /internal/ai/ocr      { document_id } -> re-run process_document
POST /internal/ai/validate { application_id } -> read-only validation check
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.ai.validator import validate_application
from app.auth.service_token import require_service_token
from app.dependencies import get_db
from app.models.ai_analysis import AIAnalysis
from app.models.application import Application
from app.models.document import Document
from app.ocr.engine import AbstractOcrEngine, get_ocr_engine
from app.schemas.ai import OcrRequest, OcrResponse, ValidateRequest, ValidateResponse
from app.services.storage_service import AbstractStorage, get_storage
from app.services.validation_service import process_document
from app.services.application_service import get_required_documents_for_application
from app.services.rule_engine_service import reevaluate_priorities

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/internal",
    tags=["internal"],
    dependencies=[Depends(require_service_token)],
    include_in_schema=False,
)


def _http_404(msg: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=msg)


# ── POST /internal/ai/ocr ─────────────────────────────────────────────────────

@router.post(
    "/ai/ocr",
    response_model=OcrResponse,
    include_in_schema=False,
)
def run_ocr(
    body: OcrRequest,
    db: Session = Depends(get_db),
    storage: AbstractStorage = Depends(get_storage),
    ocr: AbstractOcrEngine = Depends(get_ocr_engine),
) -> OcrResponse:
    """Re-run OCR on a document (updates/creates ai_analysis row)."""
    doc: Document | None = db.get(Document, body.document_id)
    if doc is None:
        raise _http_404("Document not found.")

    process_document(db, doc, storage, ocr)
    db.refresh(doc)

    analysis: AIAnalysis | None = (
        db.query(AIAnalysis)
        .filter(AIAnalysis.document_id == body.document_id)
        .first()
    )
    if analysis is None:
        # process_document failed and recorded nothing useful
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="OCR processing failed.",
        )

    return OcrResponse(
        extracted_text=analysis.extracted_text or "",
        extracted_fields=analysis.extracted_fields or {},
    )


# ── POST /internal/ai/validate ────────────────────────────────────────────────

@router.post(
    "/ai/validate",
    response_model=ValidateResponse,
    include_in_schema=False,
)
def run_validate(
    body: ValidateRequest,
    db: Session = Depends(get_db),
) -> ValidateResponse:
    """Read-only: run validate_application and return the result.

    This endpoint NEVER changes application status or writes any rows.
    """
    app: Application | None = db.get(Application, body.application_id)
    if app is None:
        raise _http_404("Application not found.")

    docs = (
        db.query(Document)
        .filter(Document.application_id == body.application_id)
        .all()
    )
    doc_dicts = [
        {
            "id": d.id,
            "doc_type": d.doc_type.strip().upper(),
            "validation_status": d.validation_status,
        }
        for d in docs
    ]
    required = get_required_documents_for_application(db, app)
    result = validate_application(required, doc_dicts)

    return ValidateResponse(
        validation_result=result,
        missing_docs=result["missing_docs"],
    )


@router.post(
    "/priority/reevaluate",
    include_in_schema=False,
)
def reevaluate_priority(db: Session = Depends(get_db)) -> dict[str, int]:
    """Raise overdue non-terminal applications to HIGH priority."""
    return {"updated": reevaluate_priorities(db)}
