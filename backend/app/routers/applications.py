"""Applications router."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user, require_citizen, require_officer
from app.dependencies import get_db
from app.models.application import Application
from app.models.department import Department
from app.models.officer import Officer
from app.models.role import Role
from app.models.user import User
from app.schemas.application import (
    ApplicationCreateRequest,
    ApplicationDetailResponse,
    ApplicationResponse,
    StatusHistoryEntry,
    StatusUpdateRequest,
)
from app.services import application_service as svc
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


def _to_response(app: Application) -> ApplicationResponse:
    return ApplicationResponse(
        id=app.id,
        citizen_id=app.citizen_id,
        department_id=app.department_id,
        service_type=app.service_type,
        status=app.status.value,
        priority=app.priority,
        created_at=app.created_at,
        updated_at=app.updated_at,
    )


def _to_detail(app: Application, history) -> ApplicationDetailResponse:
    return ApplicationDetailResponse(
        id=app.id,
        citizen_id=app.citizen_id,
        department_id=app.department_id,
        service_type=app.service_type,
        status=app.status.value,
        priority=app.priority,
        created_at=app.created_at,
        updated_at=app.updated_at,
        status_history=[
            StatusHistoryEntry(
                from_status=h.from_status,
                to_status=h.to_status,
                changed_by=h.changed_by,
                changed_at=h.changed_at,
                remarks=h.remarks,
            )
            for h in history
        ],
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
    )
    logger.info("Application %d created by citizen %d", app.id, current_user.id)
    return _to_response(app)


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
    return _to_detail(app, history)


# ── GET /applications ─────────────────────────────────────────────────────────

@router.get(
    "",
    response_model=list[ApplicationResponse],
    summary="List applications (visibility-filtered)",
)
def list_applications(
    status_filter: str | None = Query(default=None, alias="status"),
    priority: int | None = Query(default=None),
    department_id: int | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[ApplicationResponse]:
    role = _role_name(db, current_user)
    apps = svc.list_applications(
        db,
        viewer_role=role,
        viewer_user_id=current_user.id,
        status=status_filter,
        priority=priority,
        department_id=department_id,
        limit=limit,
        offset=offset,
    )
    return [_to_response(a) for a in apps]


# ── PATCH /applications/{id}/status ──────────────────────────────────────────

@router.patch(
    "/{application_id}/status",
    response_model=ApplicationDetailResponse,
    summary="Change application status (officer only)",
)
def update_status(
    application_id: int,
    payload: StatusUpdateRequest,
    current_user: User = Depends(require_officer),
    db: Session = Depends(get_db),
) -> ApplicationDetailResponse:
    app = svc.get_application_by_id(db, application_id)

    # Officer must have an active assignment; return 404 if not
    if app is None or svc.get_active_assignment_for_officer(db, app.id, current_user.id) is None:
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
            detail=f"Transition {old_status} \u2192 {new_status} is not defined.",
        )
    if allowed_actor != "officer":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Transition {old_status} \u2192 {new_status} requires "
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

    history = svc.get_status_history(db, application_id)
    return _to_detail(updated, history)
