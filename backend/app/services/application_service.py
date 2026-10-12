"""Application service layer — all DB writes go through here.

Rules
-----
- Every status change happens in ONE transaction: lock row → validate → update
  status + updated_at → insert history row.  If any step fails, nothing saved.
- changed_by is always a real user id (never NULL — the DB has NOT NULL on that
  column).  For "system" transitions triggered by the create flow, we pass the
  citizen's user id as the actor so the constraint is satisfied.
- The state machine is the single source of truth for legal transitions.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.application import Application, ApplicationStatus
from app.models.application_status_history import ApplicationStatusHistory
from app.models.assignment import Assignment
from app.models.department import Department
from app.models.officer import Officer
from app.services.state_machine import InvalidTransitionError, validate_transition

logger = logging.getLogger(__name__)


# ── helpers ───────────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _add_history(
    db: Session,
    application_id: int,
    from_status: str | None,
    to_status: str,
    changed_by: int,
    remarks: str | None = None,
) -> ApplicationStatusHistory:
    row = ApplicationStatusHistory(
        application_id=application_id,
        from_status=from_status,
        to_status=to_status,
        changed_by=changed_by,
        remarks=remarks,
    )
    db.add(row)
    return row


# ── public API ────────────────────────────────────────────────────────────────

def create_application(
    db: Session,
    *,
    citizen_id: int,
    service_type: str,
    department_id: int,
) -> Application:
    """Create an application and immediately advance it to DOCUMENT_PENDING.

    Everything happens in a single transaction:
      1. Insert application (SUBMITTED).
      2. History row: NULL → SUBMITTED, changed_by = citizen.
      3. Advance to DOCUMENT_PENDING (system actor, changed_by = citizen because
         the column is NOT NULL and citizen triggered the flow).
      4. History row: SUBMITTED → DOCUMENT_PENDING, changed_by = citizen.
      5. Commit.
    """
    app = Application(
        citizen_id=citizen_id,
        department_id=department_id,
        service_type=service_type,
        status=ApplicationStatus.SUBMITTED,
        priority=0,          # MEDIUM default
        is_duplicate_flag=False,
    )
    db.add(app)
    db.flush()  # get app.id

    # History: initial creation
    _add_history(
        db,
        application_id=app.id,
        from_status=None,
        to_status=ApplicationStatus.SUBMITTED.value,
        changed_by=citizen_id,
    )

    # System auto-advance to DOCUMENT_PENDING
    # actor="system" is validated, but changed_by uses citizen_id (NOT NULL constraint)
    validate_transition(
        ApplicationStatus.SUBMITTED.value,
        ApplicationStatus.DOCUMENT_PENDING.value,
        actor="system",
    )
    app.status = ApplicationStatus.DOCUMENT_PENDING
    app.updated_at = func.now()

    _add_history(
        db,
        application_id=app.id,
        from_status=ApplicationStatus.SUBMITTED.value,
        to_status=ApplicationStatus.DOCUMENT_PENDING.value,
        changed_by=citizen_id,
    )

    db.commit()
    db.refresh(app)
    return app


def change_status(
    db: Session,
    *,
    application_id: int,
    new_status: str,
    actor: str,
    changed_by: int,
    remarks: str | None = None,
) -> Application:
    """Transition an application to *new_status* inside a single transaction.

    Locks the row with SELECT … FOR UPDATE so concurrent requests don't race.
    Raises InvalidTransitionError if the transition is illegal.
    """
    app = (
        db.query(Application)
        .filter(Application.id == application_id)
        .with_for_update()
        .first()
    )
    if app is None:
        raise ValueError(f"Application {application_id} not found.")

    old_status = app.status.value

    validate_transition(old_status, new_status, actor=actor, remarks=remarks)

    app.status = ApplicationStatus(new_status)
    app.updated_at = func.now()

    _add_history(
        db,
        application_id=app.id,
        from_status=old_status,
        to_status=new_status,
        changed_by=changed_by,
        remarks=remarks,
    )

    db.commit()
    db.refresh(app)
    return app


# ── read helpers ──────────────────────────────────────────────────────────────

def get_application_by_id(db: Session, application_id: int) -> Application | None:
    return db.get(Application, application_id)


def get_status_history(
    db: Session, application_id: int
) -> list[ApplicationStatusHistory]:
    return (
        db.query(ApplicationStatusHistory)
        .filter(ApplicationStatusHistory.application_id == application_id)
        .order_by(ApplicationStatusHistory.changed_at.asc())
        .all()
    )


def get_active_assignment_for_officer(
    db: Session, application_id: int, officer_user_id: int
) -> Assignment | None:
    """Return the active assignment if this officer is assigned, else None."""
    officer = (
        db.query(Officer)
        .filter(Officer.user_id == officer_user_id)
        .first()
    )
    if officer is None:
        return None
    return (
        db.query(Assignment)
        .filter(
            Assignment.application_id == application_id,
            Assignment.officer_id == officer.id,
            Assignment.unassigned_at.is_(None),
        )
        .first()
    )


def list_applications(
    db: Session,
    *,
    viewer_role: str,
    viewer_user_id: int,
    status: str | None = None,
    priority: int | None = None,
    department_id: int | None = None,
    limit: int = 20,
    offset: int = 0,
) -> list[Application]:
    """Return applications visible to the viewer, with optional filters."""
    q = db.query(Application)

    if viewer_role == "citizen":
        q = q.filter(Application.citizen_id == viewer_user_id)

    elif viewer_role == "officer":
        officer = (
            db.query(Officer)
            .filter(Officer.user_id == viewer_user_id)
            .first()
        )
        if officer is None:
            return []
        assigned_ids = (
            db.query(Assignment.application_id)
            .filter(
                Assignment.officer_id == officer.id,
                Assignment.unassigned_at.is_(None),
            )
        )
        q = q.filter(Application.id.in_(assigned_ids))

    elif viewer_role == "dept_head":
        officer = (
            db.query(Officer)
            .filter(Officer.user_id == viewer_user_id)
            .first()
        )
        if officer is None:
            return []
        q = q.filter(Application.department_id == officer.department_id)

    # admin: no filter

    if status:
        q = q.filter(Application.status == ApplicationStatus(status))
    if priority is not None:
        q = q.filter(Application.priority == priority)
    if department_id is not None:
        q = q.filter(Application.department_id == department_id)

    return (
        q.order_by(Application.created_at.desc())
        .limit(min(limit, 100))
        .offset(offset)
        .all()
    )
