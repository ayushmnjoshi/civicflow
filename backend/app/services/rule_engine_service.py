"""Orchestration for deterministic priority and duplicate rules."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.ai.duplicates import is_duplicate
from app.ai.priority import WAITING_HIGH_HOURS, decide_priority
from app.constants import Priority
from app.models.application import Application, ApplicationStatus
from app.models.department import Department
from app.models.user import User

logger = logging.getLogger(__name__)


def _age_hours(created_at: datetime | None, now: datetime | None = None) -> float:
    """Return a timezone-safe application age in hours."""
    if created_at is None:
        return 0
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return (current - created_at).total_seconds() / 3600


def apply_rules_on_create(
    db: Session,
    application: Application,
    citizen: User,
    urgent: bool,
) -> None:
    """Apply creation rules without allowing a rule failure to block submission."""
    application.priority = Priority.MEDIUM.value
    application.is_duplicate_flag = False
    try:
        department = db.get(Department, application.department_id)
        department_service_type = department.service_type if department else None
        priority, priority_reason = decide_priority(
            urgent=urgent,
            waiting_hours=_age_hours(application.created_at),
            department_service_type=department_service_type,
        )
        application.priority = priority.value
        logger.info(
            "application_id=%d reason_code=%s", application.id, priority_reason
        )

        candidates = (
            db.query(Application)
            .filter(
                Application.department_id == application.department_id,
                Application.status != ApplicationStatus.REJECTED,
                Application.id < application.id,
            )
            .all()
        )
        application.is_duplicate_flag = any(
            candidate_citizen is not None
            and is_duplicate(application, citizen, candidate, candidate_citizen)
            for candidate in candidates
            if (candidate_citizen := db.get(User, candidate.citizen_id)) is not None
        )
        duplicate_reason = (
            "duplicate_match" if application.is_duplicate_flag else "no_duplicate_match"
        )
        logger.info(
            "application_id=%d reason_code=%s", application.id, duplicate_reason
        )
    except Exception:
        application.priority = Priority.MEDIUM.value
        application.is_duplicate_flag = False
        logger.exception(
            "application_id=%d reason_code=rule_engine_error", application.id
        )


def reevaluate_priorities(db: Session) -> int:
    """Raise eligible older applications to HIGH; never lower a priority."""
    now = datetime.now(timezone.utc)
    candidates = (
        db.query(Application)
        .filter(
            Application.status.notin_(
                [ApplicationStatus.APPROVED, ApplicationStatus.REJECTED]
            )
        )
        .all()
    )
    updated = 0
    for application in candidates:
        if (
            _age_hours(application.created_at, now) >= WAITING_HIGH_HOURS
            and application.priority < Priority.HIGH.value
        ):
            application.priority = Priority.HIGH.value
            application.updated_at = func.now()
            updated += 1
            logger.info(
                "application_id=%d reason_code=waiting_time", application.id
            )
    db.commit()
    return updated
