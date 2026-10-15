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
from app.models.assignment import Assignment
from app.models.document import Document
from app.models.notification import Notification
from app.models.officer import Officer
from app.models.role import Role
from app.models.user import User
from app.ocr.engine import AbstractOcrEngine, get_ocr_engine
from app.schemas.ai import OcrRequest, OcrResponse, ValidateRequest, ValidateResponse
from app.schemas.notification import NotificationSendRequest, NotificationSendResponse
from app.services.notification_service import get_provider
from app.services.storage_service import AbstractStorage, get_storage
from app.services.validation_service import process_document
from app.services.application_service import get_required_documents_for_application
from app.services.rule_engine_service import reevaluate_priorities
from app.services.assignment_service import run_bulk_assign
from app.services.workflow_service import (
    pending_check,
    create_escalation,
)
from app.schemas.workflow import (
    PendingCheckItem,
    EscalationCreateRequest,
    EscalationCreateResponse,
)

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


# ── POST /internal/assignments/run ────────────────────────────────────────────

@router.post(
    "/assignments/run",
    include_in_schema=False,
)
def run_assignments(db: Session = Depends(get_db)) -> dict[str, int]:
    """Assign all VALIDATED applications that have no active assignment.

    Returns {"assigned": n, "still_unassigned": m}.
    n8n can call this on a schedule.
    """
    return run_bulk_assign(db)


# ── POST /internal/notifications/send ────────────────────────────────────────

def _get_role_name(db: Session, user: User) -> str:
    role = db.get(Role, user.role_id)
    return role.name if role else ""


@router.post(
    "/notifications/send",
    response_model=NotificationSendResponse,
    include_in_schema=False,
)
def send_notification(
    body: NotificationSendRequest,
    db: Session = Depends(get_db),
) -> NotificationSendResponse:
    """Insert, dispatch and confirm a notification.

    channel must be "email"; message 1-1000 chars (validated by schema).
    Recipient must be the application's citizen, an actively-assigned officer,
    or a user with role dept_head or admin.  All others → 403.
    """
    # ── channel guard ─────────────────────────────────────────────
    if body.channel != "email":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="channel must be 'email'.",
        )

    # ── look up application ───────────────────────────────────────
    application: Application | None = db.get(Application, body.application_id)
    if application is None:
        raise _http_404("Application not found.")

    # ── look up recipient ─────────────────────────────────────────
    recipient: User | None = db.get(User, body.recipient_id)
    if recipient is None:
        raise _http_404("Recipient not found.")

    # ── authorization check ───────────────────────────────────────
    recipient_role = _get_role_name(db, recipient)

    allowed = False
    if recipient.id == application.citizen_id:
        allowed = True
    elif recipient_role in ("dept_head", "admin"):
        allowed = True
    elif recipient_role == "officer":
        # Check for an active assignment
        officer = db.query(Officer).filter(Officer.user_id == recipient.id).first()
        if officer is not None:
            assignment = (
                db.query(Assignment)
                .filter(
                    Assignment.application_id == body.application_id,
                    Assignment.officer_id == officer.id,
                    Assignment.unassigned_at.is_(None),
                )
                .first()
            )
            if assignment is not None:
                allowed = True

    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Recipient is not authorised to receive notifications for this application.",
        )

    # ── insert notification row with status=pending ───────────────
    notif = Notification(
        application_id=body.application_id,
        recipient_id=body.recipient_id,
        channel=body.channel,
        message=body.message,
        status="pending",
    )
    db.add(notif)
    db.commit()
    db.refresh(notif)

    # ── call the provider ─────────────────────────────────────────
    provider = get_provider()
    result_status, _reason = provider.send(
        notification_id=notif.id,
        application_id=body.application_id,
        recipient_email=recipient.email,
        message=body.message,
    )

    # ── update the row ────────────────────────────────────────────
    from datetime import datetime, timezone

    notif.status = result_status
    if result_status == "sent":
        notif.sent_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(notif)

    if result_status != "sent":
        logger.error(
            "notification_id=%d status=failed reason_code=%s",
            notif.id,
            _reason,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Notification delivery failed.",
        )

    return NotificationSendResponse(notification_id=notif.id, status="sent")


# ── GET /internal/applications/pending-check ──────────────────────────────────

@router.get(
    "/applications/pending-check",
    response_model=list[PendingCheckItem],
    include_in_schema=False,
)
def get_pending_check(
    kind: str,
    threshold_hours: int,
    db: Session = Depends(get_db),
) -> list[PendingCheckItem]:
    """Return non-terminal applications that have been in their current status
    for at least threshold_hours whole hours.

    kind: "reminder" | "escalation"
    threshold_hours: 1–8760
    """
    if kind not in ("reminder", "escalation"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="kind must be 'reminder' or 'escalation'.",
        )
    if not (1 <= threshold_hours <= 8760):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="threshold_hours must be between 1 and 8760.",
        )

    results = pending_check(db, kind=kind, threshold_hours=threshold_hours)
    return [PendingCheckItem(**r) for r in results]


# ── POST /internal/escalations ────────────────────────────────────────────────

@router.post(
    "/escalations",
    response_model=EscalationCreateResponse,
    include_in_schema=False,
)
def create_escalation_endpoint(
    body: EscalationCreateRequest,
    db: Session = Depends(get_db),
) -> EscalationCreateResponse:
    """Create (or return existing) unresolved escalation for an application.

    Idempotent: a second call with the same application returns the same
    escalation_id and does not insert a new row.
    """
    try:
        escalation = create_escalation(
            db,
            application_id=body.application_id,
            reason=body.reason,
        )
    except ValueError as exc:
        reason = str(exc)
        if reason == "not_found":
            raise _http_404("Application not found.")
        if reason == "final":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Application is already in a final status (APPROVED or REJECTED).",
            )
        if reason == "no_target":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="no escalation target",
            )
        raise

    return EscalationCreateResponse(
        escalation_id=escalation.id,
        escalated_to_user_id=escalation.escalated_to,
    )
