"""Documents router.

POST /applications/{id}/documents  — citizen upload (multipart)
GET  /documents/{id}               — stream download (owner / assigned officer / admin)
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
from app.config import settings
from app.constants import AuditAction, AuditEntity
from app.dependencies import get_db
from app.models.ai_analysis import AIAnalysis
from app.models.document import Document
from app.models.role import Role
from app.models.user import User
from app.ocr.engine import AbstractOcrEngine, get_ocr_engine
from app.schemas.document import DocumentAnalysisResponse, DocumentUploadResponse
from app.services import document_service as svc
from app.services.audit_service import record_standalone as audit_standalone
from app.services.document_service import FileValidationError
from app.services.storage_service import AbstractStorage, StorageError, get_storage

logger = logging.getLogger(__name__)
router = APIRouter(tags=["documents"])


# ── helpers ───────────────────────────────────────────────────────────────────

def _role_name(db: Session, user: User) -> str:
    role = db.get(Role, user.role_id)
    return role.name if role else ""


def _http_400(msg: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=msg)


def _http_403() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="You do not have permission to perform this action.",
    )


def _http_404(msg: str = "Not found.") -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=msg)


def _http_503(msg: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=msg)


# ── POST /applications/{id}/documents ────────────────────────────────────────

@router.post(
    "/applications/{application_id}/documents",
    status_code=status.HTTP_201_CREATED,
    response_model=DocumentUploadResponse,
    summary="Upload a document for an application (citizen only)",
)
def upload_document(
    application_id: int,
    file: UploadFile = File(...),
    doc_type: str = Form(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    storage: AbstractStorage = Depends(get_storage),
    ocr: AbstractOcrEngine = Depends(get_ocr_engine),
) -> DocumentUploadResponse:
    # ── RBAC: only citizens may upload ───────────────────────────────────────
    role = _role_name(db, current_user)
    if role != "citizen":
        raise _http_403()

    # ── Validate doc_type via schema ──────────────────────────────────────────
    from app.schemas.document import DocumentUploadRequest
    from pydantic import ValidationError as _PydanticValidationError

    try:
        parsed = DocumentUploadRequest(doc_type=doc_type)
    except _PydanticValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=exc.errors(),
        )
    clean_doc_type = parsed.doc_type

    # ── Read file (capped at max+1 bytes to detect oversized without full load)
    max_bytes = settings.max_upload_mb * 1024 * 1024
    file_data = file.file.read(max_bytes + 1)
    raw_filename = file.filename or "upload"

    try:
        doc = svc.upload_document(
            db,
            storage,
            ocr=ocr,
            application_id=application_id,
            citizen_id=current_user.id,
            raw_filename=raw_filename,
            file_data=file_data,
            doc_type=clean_doc_type,
            max_upload_mb=settings.max_upload_mb,
        )
    except LookupError as exc:
        raise _http_404(str(exc))
    except PermissionError:
        raise _http_403()
    except FileValidationError as exc:
        raise _http_400(str(exc))
    except StorageError as exc:
        logger.error("Storage failure during upload: %s", exc)
        raise _http_503("Storage service unavailable. Please try again later.")

    logger.info(
        "Document %d uploaded for application %d by citizen %d",
        doc.id, application_id, current_user.id,
    )
    return DocumentUploadResponse(document_id=doc.id, storage_ref=doc.storage_ref)


# ── GET /documents/{id} ───────────────────────────────────────────────────────

#: Fixed Content-Type map — based on stored extension, ignores any client header
_CONTENT_TYPE: dict[str, str] = {
    "pdf":  "application/pdf",
    "jpg":  "image/jpeg",
    "jpeg": "image/jpeg",
    "png":  "image/png",
}


@router.get(
    "/documents/{document_id}",
    summary="Stream-download a document (owner / assigned officer / admin)",
)
def download_document(
    document_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    storage: AbstractStorage = Depends(get_storage),
) -> StreamingResponse:
    doc: Document | None = db.get(Document, document_id)
    if doc is None:
        raise _http_404("Document not found.")

    role = _role_name(db, current_user)
    if not svc.can_access_document(db, doc, current_user, role):
        raise _http_403()

    # Access granted — write audit row (standalone: must not block the download)
    try:
        audit_standalone(
            db,
            user_id=current_user.id,
            action=AuditAction.DOCUMENT_VIEW,
            entity_type=AuditEntity.DOCUMENT,
            entity_id=document_id,
            details={"document_id": document_id},
        )
    except Exception:
        logger.exception("audit_callsite_failed action=document.view document_id=%d", document_id)

    # Determine Content-Type from stored key extension
    key = doc.storage_ref
    ext = key.rsplit(".", 1)[-1].lower() if "." in key else ""
    content_type = _CONTENT_TYPE.get(ext, "application/octet-stream")

    try:
        stream = svc.stream_document(storage, key)
    except StorageError as exc:
        logger.error("Storage failure during download of doc %d: %s", document_id, exc)
        raise _http_503("Storage service unavailable.")

    return StreamingResponse(
        stream,
        media_type=content_type,
        headers={
            "Content-Disposition": "inline",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )


# ── GET /documents/{id}/analysis ──────────────────────────────────────────────

@router.get(
    "/documents/{document_id}/analysis",
    response_model=DocumentAnalysisResponse,
    summary="View OCR analysis result for a document (assigned officer / admin only)",
)
def get_document_analysis(
    document_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> DocumentAnalysisResponse:
    """Return OCR/validation analysis for *document_id*.

    Access rules:
    - Admin: always allowed.
    - Officer: only if they have an active assignment to the document's application.
    - Everyone else (citizen, dept_head without assignment, unauthenticated): 403 or 401.

    A document with no analysis row yet returns 404 in the standard error shape.
    extracted_text is NEVER returned.
    """
    doc: Document | None = db.get(Document, document_id)
    if doc is None:
        raise _http_404("Document not found.")

    role = _role_name(db, current_user)

    # Citizens are never allowed — even the owning citizen
    if role not in ("officer", "admin", "dept_head"):
        raise _http_403()

    # Officers must have an active assignment to the document's application
    if role in ("officer", "dept_head"):
        if not svc.can_access_document(db, doc, current_user, "officer"):
            raise _http_403()

    # Access granted — write audit row (standalone: must not block)
    try:
        audit_standalone(
            db,
            user_id=current_user.id,
            action=AuditAction.DOCUMENT_ANALYSIS_VIEW,
            entity_type=AuditEntity.DOCUMENT,
            entity_id=document_id,
            details={"document_id": document_id},
        )
    except Exception:
        logger.exception("audit_callsite_failed action=document.analysis_view document_id=%d", document_id)

    # Fetch analysis row — no extracted_text returned
    analysis: AIAnalysis | None = (
        db.query(AIAnalysis)
        .filter(AIAnalysis.document_id == document_id)
        .first()
    )
    if analysis is None:
        raise _http_404("No analysis available for this document yet.")

    return DocumentAnalysisResponse(
        document_id=doc.id,
        doc_type=doc.doc_type,
        validation_status=doc.validation_status,
        extracted_fields=analysis.extracted_fields,
        validation_result=analysis.validation_result,
        processed_at=analysis.processed_at,
    )
