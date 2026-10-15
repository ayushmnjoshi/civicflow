"""Workflow service — pending-check query and escalation CRUD.

Called by /internal/applications/pending-check and /internal/escalations.
No new tables or columns — uses existing notifications, escalations,
application_status_history, assignments, officers, users, roles.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.application import Application, ApplicationStatus
from app.models.application_status_history import ApplicationStatusHistory
from app.models.assignment import Assignment
from app.models.escalation import Escalation
from app.models.notification import Notification
from app.models.officer import Officer
from app.models.role import Role
from app.models.user import User

logger = logging.getLogger(__name__)

# Statuses that are considered "final" (terminal)
_FINAL = frozenset({"APPROVED", "REJECTED"})

# Cap result set size
_MAX_RESULTS = 500


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _make_aware(dt: datetime) -> datetime:
    """Attach UTC timezone if *dt* is naive."""
    if dt is None:
        return _now_utc()
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# ── pending-check ─────────────────────────────────────────────────────────────

def pending_check(
    db: Session,
    *,
    kind: str,
    threshold_hours: int,
) -> list[dict]:
    """Return non-terminal applications where hours_in_status >= threshold_hours.

    kind="reminder":   skip if a "sent" notification exists within the last
                       threshold_hours.
    kind="escalation": skip if an unresolved escalation (resolved_at IS NULL)
                       exists.

    Result capped at 500, ordered by hours_in_status descending.
    """
    now = _now_utc()

    # All non-terminal applications
    apps = (
        db.query(Application)
        .filter(Application.status.notin_([ApplicationStatus.APPROVED, ApplicationStatus.REJECTED]))
        .all()
    )

    results: list[dict] = []

    for app in apps:
        # Latest history row for this application
        latest_history: ApplicationStatusHistory | None = (
            db.query(ApplicationStatusHistory)
            .filter(ApplicationStatusHistory.application_id == app.id)
            .order_by(ApplicationStatusHistory.changed_at.desc())
            .first()
        )

        if latest_history is None:
            # No history rows → can't determine time in status; skip
            continue

        changed_at = _make_aware(latest_history.changed_at)
        elapsed_seconds = (now - changed_at).total_seconds()
        hours_in_status = int(elapsed_seconds // 3600)  # floor

        if hours_in_status < threshold_hours:
            continue

        # kind-specific suppression check
        if kind == "reminder":
            # Suppress if there is a "sent" notification within last threshold_hours
            cutoff = now
            # We need sent_at >= (now - threshold_hours hours) AND status="sent"
            from datetime import timedelta
            window_start = now - timedelta(hours=threshold_hours)
            recent_sent = (
                db.query(Notification)
                .filter(
                    Notification.application_id == app.id,
                    Notification.status == "sent",
                    Notification.sent_at >= window_start,
                )
                .first()
            )
            if recent_sent is not None:
                continue

        elif kind == "escalation":
            # Suppress if there is an unresolved escalation
            unresolved = (
                db.query(Escalation)
                .filter(
                    Escalation.application_id == app.id,
                    Escalation.resolved_at.is_(None),
                )
                .first()
            )
            if unresolved is not None:
                continue

        # Find the active assignment's officer user_id (if any)
        active_assignment: Assignment | None = (
            db.query(Assignment)
            .filter(
                Assignment.application_id == app.id,
                Assignment.unassigned_at.is_(None),
            )
            .first()
        )
        assigned_officer_user_id: int | None = None
        if active_assignment is not None:
            officer: Officer | None = db.get(Officer, active_assignment.officer_id)
            if officer is not None:
                assigned_officer_user_id = officer.user_id

        results.append(
            {
                "application_id": app.id,
                "status": app.status.value,
                "citizen_id": app.citizen_id,
                "department_id": app.department_id,
                "assigned_officer_user_id": assigned_officer_user_id,
                "hours_in_status": hours_in_status,
            }
        )

    # Sort by hours_in_status descending
    results.sort(key=lambda r: r["hours_in_status"], reverse=True)

    return results[:_MAX_RESULTS]


# ── escalations ───────────────────────────────────────────────────────────────

def _find_escalation_target(db: Session, department_id: int) -> User | None:
    """Return the dept_head with the lowest officers.id in the given department,
    or the admin with the lowest users.id as fallback.
    Returns None if neither exists.
    """
    # Try: dept_head role
    dept_head_role: Role | None = db.query(Role).filter(Role.name == "dept_head").first()
    if dept_head_role is not None:
        # Officers in the same department with role dept_head, ordered by officer.id asc
        target_officer: Officer | None = (
            db.query(Officer)
            .join(User, User.id == Officer.user_id)
            .filter(
                Officer.department_id == department_id,
                User.role_id == dept_head_role.id,
            )
            .order_by(Officer.id.asc())
            .first()
        )
        if target_officer is not None:
            return db.get(User, target_officer.user_id)

    # Fallback: admin with lowest user id
    admin_role: Role | None = db.query(Role).filter(Role.name == "admin").first()
    if admin_role is not None:
        admin_user: User | None = (
            db.query(User)
            .filter(User.role_id == admin_role.id)
            .order_by(User.id.asc())
            .first()
        )
        if admin_user is not None:
            return admin_user

    return None


def create_escalation(
    db: Session,
    *,
    application_id: int,
    reason: str,
) -> Escalation:
    """Create or return the existing unresolved escalation for an application.

    Returns the existing escalation if one is already unresolved (idempotent).
    Raises:
        ValueError("not_found")   if application does not exist
        ValueError("final")       if application is APPROVED or REJECTED
        ValueError("no_target")   if no eligible escalation target exists
    """
    app: Application | None = db.get(Application, application_id)
    if app is None:
        raise ValueError("not_found")

    if app.status.value in _FINAL:
        raise ValueError("final")

    # Idempotent: return existing unresolved escalation if any
    existing: Escalation | None = (
        db.query(Escalation)
        .filter(
            Escalation.application_id == application_id,
            Escalation.resolved_at.is_(None),
        )
        .first()
    )
    if existing is not None:
        return existing

    # Determine escalation target
    target_user = _find_escalation_target(db, app.department_id)
    if target_user is None:
        raise ValueError("no_target")

    now = _now_utc()
    escalation = Escalation(
        application_id=application_id,
        escalated_to=target_user.id,
        reason=reason,
        escalated_at=now,
        resolved_at=None,
    )
    db.add(escalation)
    db.commit()
    db.refresh(escalation)
    logger.info(
        "escalation_id=%d application_id=%d escalated_to_user_id=%d",
        escalation.id,
        application_id,
        target_user.id,
    )
    return escalation


def resolve_escalations_for_application(db: Session, application_id: int) -> None:
    """Set resolved_at = now() on all unresolved escalations for *application_id*.

    Called within the same transaction as the status-history row write.
    No commit here — the caller owns the transaction.
    """
    now = _now_utc()
    rows: list[Escalation] = (
        db.query(Escalation)
        .filter(
            Escalation.application_id == application_id,
            Escalation.resolved_at.is_(None),
        )
        .all()
    )
    for row in rows:
        row.resolved_at = now
        logger.info(
            "escalation_id=%d application_id=%d reason_code=resolved_on_final",
            row.id,
            application_id,
        )
