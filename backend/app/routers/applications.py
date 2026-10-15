"""Applications router."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user, require_citizen, require_officer
from app.constants import AuditAction, AuditEntity, Priority
from app.dependencies import get_db
from app.models.application import Application, ApplicationStatus
from app.models.department import Department
from app.models.officer import Officer
from app.models.role import Role
from app.models.user import User
from app.schemas.application import (
    ApplicationCreateRequest,
    ApplicationDetailResponse,
    ApplicationResponse,
    DecisionRequest,
    DecisionResponse,
    RequestDocumentsRequest,
    RequestDocumentsResponse,
    StatusHistoryEntry,
    StatusUpdateRequest,
)
from app.schemas.document import DocumentSummary
from app.services import application_service as svc
from app.services.audit_service import record as audit_record
from app.services.state_machine import (
    InvalidTransitionError,
    TRANSITIONS,
    validate_transition,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/applications", tags=["applications"])


# ── helpers ───────────────────────────────────────────────────────────────────

def _role_name(db: Session, user: User) -> str:
    role = db.get(Role, user.role_id)
    return role.name if role else ""


def _get_applicant_name(db: Session, app: Application, viewer_role: str) -> str | None:
    """Return the citizen's full_name for staff viewers; None for citizens."""
    if viewer_role not in ("officer", "dept_head", "admin"):
        return None
    citizen = db.get(User, app.citizen_id)
    return citizen.full_name if citizen else None


def _to_response(app: Application, viewer_role: str) -> ApplicationResponse:
    return ApplicationResponse(
        id=app.id,
        citizen_id=app.citizen_id,
        department_id=app.department_id,
        service_type=app.service_type,
        status=app.status.value,
        priority=Priority(app.priority).name,
        is_duplicate_flag=(
            app.is_duplicate_flag
            if viewer_role in ("officer", "dept_head", "admin")
            else None
        ),
        created_at=app.created_at,
        updated_at=app.updated_at,
    )


def _to_detail(
    db: Session,
    app: Application,
    history,
    viewer_role: str,
) -> ApplicationDetailResponse:
    # Build history entries with actor resolution
    history_entries = []
    for h in history:
        actor_role, actor_name = svc.resolve_history_actor(db, h, viewer_role)
        history_entries.append(
            StatusHistoryEntry(
                from_status=h.from_status,
                to_status=h.to_status,
                actor_role=actor_role,
                actor_name=actor_name,
                changed_at=h.changed_at,
                remarks=h.remarks,
            )
        )

    # Build document summary list
    docs = svc.get_documents_for_application(db, app.id)
    doc_summaries = [
        DocumentSummary(
            id=d.id,
            doc_type=d.doc_type,
            original_filename=d.original_filename,
            validation_status=d.validation_status,
            uploaded_at=d.uploaded_at,
        )
        for d in docs
    ]

    # Required / missing / invalid
    required = svc.get_required_documents_for_application(db, app)
    missing = svc.get_missing_documents(required, docs)
    invalid = svc.get_invalid_documents(required, docs)

    return ApplicationDetailResponse(
        id=app.id,
        citizen_id=app.citizen_id,
        department_id=app.department_id,
        service_type=app.service_type,
        status=app.status.value,
        priority=Priority(app.priority).name,
        is_duplicate_flag=(
            app.is_duplicate_flag
            if viewer_role in ("officer", "dept_head", "admin")
            else None
        ),
        created_at=app.created_at,
        updated_at=app.updated_at,
        applicant_name=_get_applicant_name(db, app, viewer_role),
        status_history=history_entries,
        documents=doc_summaries,
        required_documents=required,
        missing_documents=missing,
        invalid_documents=invalid,
    )


def _visible_to(db: Session, app: Application, user: User, role: str) -> bool:
    """Return True if *user* may see *app*."""
    if role == "citizen":
        return app.citizen_id == user.id
    if role == "admin":
        return True
    if role == "officer":
        return svc.get_active_assignment_for_officer(db, app.id, user.id) is not None
    if role == "dept_head":
        officer = db.query(Officer).filter(Officer.user_id == user.id).first()
        return officer is not None and app.department_id == officer.department_id
    return False


def _require_active_assignment(
    db: Session, app: Application | None, officer_user: User
) -> Application:
    """Return the application or raise 404/403 as appropriate for officer access."""
    if app is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Application not found.",
        )
    assignment = svc.get_active_assignment_for_officer(db, app.id, officer_user.id)
    if assignment is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to perform this action.",
        )
    return app


# ── POST /applications ────────────────────────────────────────────────────────

@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=ApplicationResponse,
    summary="Submit a new application (citizen only)",
)
def create_application(
    payload: ApplicationCreateRequest,
    current_user: User = Depends(require_citizen),
    db: Session = Depends(get_db),
) -> ApplicationResponse:
    dept = db.get(Department, payload.department_id)
    if dept is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Department {payload.department_id} not found.",
        )

    app = svc.create_application(
        db,
        citizen_id=current_user.id,
        service_type=payload.service_type,
        department_id=payload.department_id,
        urgent=payload.urgent,
    )
    logger.info("application_id=%d reason_code=created", app.id)
    return _to_response(app, "citizen")


# ── GET /applications/{id} ────────────────────────────────────────────────────

@router.get(
    "/{application_id}",
    response_model=ApplicationDetailResponse,
    summary="Get application detail",
)
def get_application(
    application_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApplicationDetailResponse:
    role = _role_name(db, current_user)
    app = svc.get_application_by_id(db, application_id)

    # Return 404 regardless of reason — don't leak existence
    if app is None or not _visible_to(db, app, current_user, role):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Application not found.",
        )

    history = svc.get_status_history(db, application_id)
    return _to_detail(db, app, history, role)


# ── GET /applications ─────────────────────────────────────────────────────────

@router.get(
    "",
    response_model=list[ApplicationResponse],
    summary="List applications (visibility-filtered)",
)
def list_applications(
    status_filter: str | None = Query(default=None, alias="status"),
    priority: str | None = Query(default=None, description="LOW | MEDIUM | HIGH"),
    department_id: int | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[ApplicationResponse]:
    # Validate and convert the priority label to its integer value
    priority_int: int | None = None
    if priority is not None:
        try:
            priority_int = Priority.from_label(priority).value
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            )

    role = _role_name(db, current_user)
    apps = svc.list_applications(
        db,
        viewer_role=role,
        viewer_user_id=current_user.id,
        status=status_filter,
        priority=priority_int,
        department_id=department_id,
        limit=limit,
        offset=offset,
    )
    return [_to_response(a, role) for a in apps]


# ── PATCH /applications/{id}/status ──────────────────────────────────────────

@router.patch(
    "/{application_id}/status",
    response_model=ApplicationDetailResponse,
    summary="Change application status (officer/admin)",
)
def update_status(
    application_id: int,
    payload: StatusUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApplicationDetailResponse:
    role = _role_name(db, current_user)

    # Only officer and admin can patch status
    if role not in ("officer", "admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to perform this action.",
        )

    app = svc.get_application_by_id(db, application_id)

    if role == "officer":
        # Officer must have an active assignment; 403 if not
        if app is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Application not found.",
            )
        assignment = svc.get_active_assignment_for_officer(db, app.id, current_user.id)
        if assignment is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to perform this action.",
            )
    else:
        # admin
        if app is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Application not found.",
            )

    # Validate remarks before hitting the state machine
    new_status = payload.new_status
    old_status = app.status.value
    remarks = (payload.remarks or "").strip() or None

    # Check actor for this transition
    allowed_actor = TRANSITIONS.get((old_status, new_status))
    if allowed_actor is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Transition {old_status} → {new_status} is not defined.",
        )
    if allowed_actor != "officer":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Transition {old_status} → {new_status} requires "
                f"actor '{allowed_actor}', not 'officer'."
            ),
        )

    try:
        updated = svc.change_status(
            db,
            application_id=application_id,
            new_status=new_status,
            actor="officer",
            changed_by=current_user.id,
            remarks=remarks,
        )
    except InvalidTransitionError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )

    role = _role_name(db, current_user)
    history = svc.get_status_history(db, application_id)
    return _to_detail(db, updated, history, role)


# ── POST /applications/{id}/decision ─────────────────────────────────────────

@router.post(
    "/{application_id}/decision",
    response_model=DecisionResponse,
    summary="Approve or reject an application (assigned officer only)",
)
def decide_application(
    application_id: int,
    payload: DecisionRequest,
    current_user: User = Depends(require_officer),
    db: Session = Depends(get_db),
) -> DecisionResponse:
    app = svc.get_application_by_id(db, application_id)
    app = _require_active_assignment(db, app, current_user)

    if app.status != ApplicationStatus.UNDER_REVIEW:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Application must be UNDER_REVIEW to make a decision; current status is {app.status.value}.",
        )

    new_status = "APPROVED" if payload.decision == "approve" else "REJECTED"
    remarks = payload.remarks.strip()

    try:
        updated = svc.change_status(
            db,
            application_id=application_id,
            new_status=new_status,
            actor="officer",
            changed_by=current_user.id,
            remarks=remarks,
        )
    except InvalidTransitionError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )

    logger.info(
        "application_id=%d reason_code=%s actor_id=%d",
        application_id, new_status.lower(), current_user.id,
    )
    return DecisionResponse(id=updated.id, status=updated.status.value)


# ── POST /applications/{id}/request-documents ─────────────────────────────────

@router.post(
    "/{application_id}/request-documents",
    response_model=RequestDocumentsResponse,
    summary="Request additional documents (assigned officer only)",
)
def request_documents(
    application_id: int,
    payload: RequestDocumentsRequest,
    current_user: User = Depends(require_officer),
    db: Session = Depends(get_db),
) -> RequestDocumentsResponse:
    # Known limitation: the requested doc_types are stored only in the history
    # remarks field (not in a dedicated column). This is sufficient for
    # audit purposes but does not allow programmatic retrieval of pending requests.
    app = svc.get_application_by_id(db, application_id)
    app = _require_active_assignment(db, app, current_user)

    if app.status != ApplicationStatus.UNDER_REVIEW:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Application must be UNDER_REVIEW to request documents; current status is {app.status.value}.",
        )

    doc_list = ", ".join(payload.doc_types)
    remarks = f"Requested: {doc_list}. Reason: {payload.reason}"

    try:
        updated = svc.change_status(
            db,
            application_id=application_id,
            new_status="ADDITIONAL_DOCUMENT_REQUIRED",
            actor="officer",
            changed_by=current_user.id,
            remarks=remarks,
        )
    except InvalidTransitionError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )

    # Audit: application.request_documents (officer, separate record after the commit)
    # change_status already committed, so we use a fresh write in the same db session.
    # We add the audit row and commit it here.
    try:
        audit_record(
            db,
            user_id=current_user.id,
            action=AuditAction.APPLICATION_REQUEST_DOCUMENTS,
            entity_type=AuditEntity.APPLICATION,
            entity_id=application_id,
            details={"doc_types": payload.doc_types},
        )
        db.commit()
    except Exception:
        logger.exception(
            "audit_failed action=request_documents application_id=%d", application_id
        )
        try:
            db.rollback()
        except Exception:
            pass

    logger.info(
        "application_id=%d reason_code=additional_document_requested actor_id=%d",
        application_id, current_user.id,
    )
    return RequestDocumentsResponse(id=updated.id, status=updated.status.value)
