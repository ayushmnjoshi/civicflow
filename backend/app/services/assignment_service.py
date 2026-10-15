"""Assignment service — auto-assign VALIDATED applications to officers.

Public API
----------
assign_application(db, application)
    Lock the application row. If it already has an active assignment, do nothing.
    Pick the officer in the same department with the fewest active (non-final)
    assignments; ties go to the lowest officers.id.
    Insert an assignments row, then transition VALIDATED -> ASSIGNED via the
    existing state machine (system actor), writing a history row.
    Returns the new Assignment row, or None if no candidate found.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.application import Application, ApplicationStatus
from app.models.application_status_history import ApplicationStatusHistory
from app.models.assignment import Assignment
from app.models.officer import Officer
from app.constants import AuditAction, AuditEntity
from app.services.audit_service import record as audit_record
from app.services.state_machine import validate_transition
from app.services.system_actor import get_system_user_id

logger = logging.getLogger(__name__)

# Statuses that count as "active" load on an officer (i.e. non-final, non-terminal)
_FINAL_STATUSES = (ApplicationStatus.APPROVED, ApplicationStatus.REJECTED)


def assign_application(db: Session, application: Application) -> Assignment | None:
    """Auto-assign *application* to the least-loaded officer in its department.

    - Locks the application row for update to prevent concurrent races.
    - If an active assignment already exists, returns None immediately.
    - Picks the officer with the fewest active (non-APPROVED/REJECTED) assignments;
      ties go to the lowest officers.id.
    - Inserts the assignment row, transitions VALIDATED -> ASSIGNED (system actor).
    - Returns the Assignment row, or None if no candidate found.
    - Never raises to caller.
    """
    try:
        return _do_assign(db, application)
    except Exception:
        logger.exception(
            "assign_application: unexpected error application_id=%d", application.id
        )
        try:
            db.rollback()
        except Exception:
            pass
        return None


def _do_assign(db: Session, application: Application) -> Assignment | None:
    """Internal implementation — may raise; caller handles rollback."""
    # Re-lock the application row
    app = (
        db.query(Application)
        .filter(Application.id == application.id)
        .with_for_update()
        .first()
    )
    if app is None:
        return None

    # Check for existing active assignment
    existing = (
        db.query(Assignment)
        .filter(
            Assignment.application_id == app.id,
            Assignment.unassigned_at.is_(None),
        )
        .first()
    )
    if existing is not None:
        # Already assigned — idempotent, do nothing
        return None

    dept_id = app.department_id

    # Find the least-loaded officer in the same department.
    # "Load" = number of active assignments on non-final applications.
    # Uses a subquery to count active assignments per officer on non-final apps.
    active_load_subq = (
        db.query(
            Assignment.officer_id,
            func.count(Assignment.id).label("load"),
        )
        .join(Application, Application.id == Assignment.application_id)
        .filter(
            Assignment.unassigned_at.is_(None),
            Application.status.notin_([s.value for s in _FINAL_STATUSES]),
        )
        .group_by(Assignment.officer_id)
        .subquery()
    )

    # All officers in the department, left-joined with their load
    candidate = (
        db.query(Officer)
        .outerjoin(active_load_subq, Officer.id == active_load_subq.c.officer_id)
        .filter(Officer.department_id == dept_id)
        .order_by(
            func.coalesce(active_load_subq.c.load, 0).asc(),
            Officer.id.asc(),
        )
        .first()
    )

    if candidate is None:
        logger.warning(
            "assign_application: no officer available application_id=%d", app.id
        )
        return None

    system_user_id = get_system_user_id(db)

    # Insert the assignment row
    assignment = Assignment(
        application_id=app.id,
        officer_id=candidate.id,
    )
    db.add(assignment)
    db.flush()

    # Transition VALIDATED -> ASSIGNED (system actor)
    old_status = app.status.value
    validate_transition(old_status, "ASSIGNED", actor="system")
    app.status = ApplicationStatus.ASSIGNED
    app.updated_at = func.now()

    db.add(ApplicationStatusHistory(
        application_id=app.id,
        from_status=old_status,
        to_status="ASSIGNED",
        changed_by=system_user_id,
        remarks=None,
    ))

    # Audit: assignment.create (system user, same transaction)
    audit_record(
        db,
        user_id=system_user_id,
        action=AuditAction.ASSIGNMENT_CREATE,
        entity_type=AuditEntity.APPLICATION,
        entity_id=app.id,
        details={"officer_id": candidate.id},
    )
    # Audit: status change ASSIGNED (system, same transaction)
    audit_record(
        db,
        user_id=system_user_id,
        action=AuditAction.APPLICATION_STATUS_CHANGE,
        entity_type=AuditEntity.APPLICATION,
        entity_id=app.id,
        details={"from_status": old_status, "to_status": "ASSIGNED"},
    )

    db.commit()
    return assignment


def run_bulk_assign(db: Session) -> dict[str, int]:
    """Assign all VALIDATED applications that have no active assignment.

    Returns {"assigned": n, "still_unassigned": m}.
    Called by POST /internal/assignments/run.
    """
    # Find all VALIDATED apps with no active assignment
    already_assigned_ids = (
        db.query(Assignment.application_id)
        .filter(Assignment.unassigned_at.is_(None))
    )

    apps_to_assign = (
        db.query(Application)
        .filter(
            Application.status == ApplicationStatus.VALIDATED,
            Application.id.notin_(already_assigned_ids),
        )
        .all()
    )

    assigned = 0
    for app in apps_to_assign:
        result = assign_application(db, app)
        if result is not None:
            assigned += 1

    # Re-count still-VALIDATED apps with no active assignment
    still_unassigned = (
        db.query(Application)
        .filter(
            Application.status == ApplicationStatus.VALIDATED,
            Application.id.notin_(
                db.query(Assignment.application_id)
                .filter(Assignment.unassigned_at.is_(None))
            ),
        )
        .count()
    )

    return {"assigned": assigned, "still_unassigned": still_unassigned}
