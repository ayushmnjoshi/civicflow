"""Application service layer — all DB writes go through here.

Rules
-----
- Every status change happens in ONE transaction: lock row → validate → update
  status + updated_at → insert history row.  If any step fails, nothing saved.
- changed_by is always a real user id — for "system" transitions the system
  user's id is used (seeded by database/seed/seed.py).
- The state machine is the single source of truth for legal transitions.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.ai.rules import get_required_doc_types
from app.constants import Priority, VALIDATION_STATUS_INVALID, VALIDATION_STATUS_ERROR
from app.models.application import Application, ApplicationStatus
from app.models.application_status_history import ApplicationStatusHistory
from app.models.assignment import Assignment
from app.models.department import Department
from app.models.document import Document
from app.models.officer import Officer
from app.models.role import Role
from app.models.user import User
from app.services.state_machine import InvalidTransitionError, validate_transition
from app.services.system_actor import get_system_user_id
from app.services.rule_engine_service import apply_rules_on_create

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
    urgent: bool = False,
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
    system_user_id = get_system_user_id(db)

    app = Application(
        citizen_id=citizen_id,
        department_id=department_id,
        service_type=service_type,
        status=ApplicationStatus.SUBMITTED,
        priority=Priority.MEDIUM.value,  # 1
        is_duplicate_flag=False,
    )
    db.add(app)
    db.flush()  # get app.id

    # Urgency is self-declared; an officer can change priority later.
    try:
        apply_rules_on_create(db, app, db.get(User, citizen_id), urgent)
    except Exception:
        # A broken rules engine must never prevent a citizen's submission.
        app.priority = Priority.MEDIUM.value
        app.is_duplicate_flag = False
        logger.exception(
            "application_id=%d reason_code=rule_engine_error", app.id
        )

    # History row 1: citizen submitted the application
    _add_history(
        db,
        application_id=app.id,
        from_status=None,
        to_status=ApplicationStatus.SUBMITTED.value,
        changed_by=citizen_id,
    )

    # System auto-advance to DOCUMENT_PENDING
    validate_transition(
        ApplicationStatus.SUBMITTED.value,
        ApplicationStatus.DOCUMENT_PENDING.value,
        actor="system",
    )
    app.status = ApplicationStatus.DOCUMENT_PENDING
    app.updated_at = func.now()

    # History row 2: system actor advanced the status
    _add_history(
        db,
        application_id=app.id,
        from_status=ApplicationStatus.SUBMITTED.value,
        to_status=ApplicationStatus.DOCUMENT_PENDING.value,
        changed_by=system_user_id,
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


# ── Document detail helpers (Step 4) ─────────────────────────────────────────

def get_documents_for_application(
    db: Session, application_id: int
) -> list[Document]:
    """Return all documents for an application ordered by id."""
    return (
        db.query(Document)
        .filter(Document.application_id == application_id)
        .order_by(Document.id.asc())
        .all()
    )


def get_required_documents_for_application(
    db: Session, app: Application
) -> list[str]:
    """Return the required doc types using the DEPARTMENT's service_type."""
    dept = db.get(Department, app.department_id)
    dept_service_type = dept.service_type if dept is not None else ""
    return [r.upper() for r in get_required_doc_types(dept_service_type)]


def get_missing_documents(
    required: list[str], documents: list[Document]
) -> list[str]:
    """Return required doc_types with no uploaded document (any status)."""
    uploaded_types = {d.doc_type.strip().upper() for d in documents}
    return [r for r in required if r not in uploaded_types]


def get_invalid_documents(
    required: list[str], documents: list[Document]
) -> list[str]:
    """Return required doc_types whose LATEST document (by id) has status INVALID or ERROR."""
    # Build: doc_type -> latest document (highest id)
    latest: dict[str, Document] = {}
    for doc in documents:
        dt = doc.doc_type.strip().upper()
        if dt not in latest or doc.id > latest[dt].id:
            latest[dt] = doc

    result: list[str] = []
    for req in required:
        doc = latest.get(req)
        if doc is not None and doc.validation_status in (
            VALIDATION_STATUS_INVALID,
            VALIDATION_STATUS_ERROR,
        ):
            result.append(req)
    return result


# ── Status history actor resolution (Step 5) ──────────────────────────────────

_SYSTEM_EMAIL = "system@civicflow.internal"


def _get_user_role_name(db: Session, user_id: int) -> str:
    """Return the role name for *user_id*, or '' if not found."""
    user = db.get(User, user_id)
    if user is None:
        return ""
    role = db.get(Role, user.role_id)
    return role.name if role else ""


def _get_user_full_name(db: Session, user_id: int) -> str | None:
    """Return the user's full_name, or None if not found."""
    user = db.get(User, user_id)
    return user.full_name if user is not None else None


def _is_system_user(db: Session, user_id: int) -> bool:
    """Return True if *user_id* is the system actor."""
    user = db.get(User, user_id)
    return user is not None and user.email == _SYSTEM_EMAIL


def resolve_history_actor(
    db: Session,
    history_row: ApplicationStatusHistory,
    viewer_role: str,
) -> tuple[str, str | None]:
    """Return (actor_role, actor_name) for a history row.

    actor_role: "citizen" | "system" | "officer" | "dept_head" | "admin"
    actor_name:
      - "System"         for the system actor
      - person's full_name  when viewer is staff (officer / dept_head / admin)
      - null             when viewer is citizen and actor is not citizen
    """
    changed_by = history_row.changed_by

    # Identify system user
    if _is_system_user(db, changed_by):
        return "system", "System"

    actor_role = _get_user_role_name(db, changed_by)

    # Actor names are staff-only.  Citizens can still see roles and the
    # system label, but never a person's name (including their own).
    is_staff_viewer = viewer_role in ("officer", "dept_head", "admin")

    if is_staff_viewer:
        name = _get_user_full_name(db, changed_by)
    else:
        # citizen viewer, non-citizen actor → hide name
        name = None

    return actor_role, name
