"""Officer router — officer-specific endpoints.

GET /officer/queue  — Returns applications actively assigned to the calling
                      officer, excluding APPROVED/REJECTED.  Role officer only.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.auth.dependencies import require_officer
from app.constants import Priority
from app.dependencies import get_db
from app.models.application import Application, ApplicationStatus
from app.models.assignment import Assignment
from app.models.officer import Officer
from app.models.user import User
from app.schemas.application import OfficerQueueItem

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/officer", tags=["officer"])

# Final statuses excluded from the queue
_FINAL_STATUSES = {
    ApplicationStatus.APPROVED.value,
    ApplicationStatus.REJECTED.value,
}

# Valid application statuses for the optional filter
_VALID_STATUSES = {s.value for s in ApplicationStatus}


@router.get(
    "/queue",
    response_model=list[OfficerQueueItem],
    summary="Get the calling officer's active application queue",
)
def get_officer_queue(
    status_filter: str | None = Query(default=None, alias="status"),
    current_user: User = Depends(require_officer),
    db: Session = Depends(get_db),
) -> list[OfficerQueueItem]:
    """Return applications actively assigned to the calling officer.

    - Excludes APPROVED and REJECTED applications.
    - Optional `status` query param filters by a specific status (422 if unknown).
    - Sorted by priority descending, then created_at ascending.
    - If the user has role=officer but no officers row, returns [].
    """
    # Validate status filter if provided
    if status_filter is not None:
        if status_filter not in _VALID_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Unknown status '{status_filter}'. Valid values: {', '.join(sorted(_VALID_STATUSES))}",
            )
        if status_filter in _FINAL_STATUSES:
            # Requesting a final status — will always be empty since we exclude finals
            return []

    # Resolve the officers row for the calling user
    officer = db.query(Officer).filter(Officer.user_id == current_user.id).first()
    if officer is None:
        # Officer role but no officers row — return empty list, not 500
        return []

    # Build the query: applications actively assigned to this officer
    q = (
        db.query(Application)
        .join(Assignment, Assignment.application_id == Application.id)
        .filter(
            Assignment.officer_id == officer.id,
            Assignment.unassigned_at.is_(None),
            Application.status.notin_([s for s in _FINAL_STATUSES]),
        )
    )

    if status_filter is not None:
        q = q.filter(Application.status == ApplicationStatus(status_filter))

    # Sort: priority DESC, created_at ASC
    apps = q.order_by(
        Application.priority.desc(),
        Application.created_at.asc(),
    ).all()

    # Resolve applicant names (staff can see them)
    result: list[OfficerQueueItem] = []
    for app in apps:
        citizen = db.get(User, app.citizen_id)
        applicant_name = citizen.full_name if citizen else None
        result.append(
            OfficerQueueItem(
                id=app.id,
                service_type=app.service_type,
                status=app.status.value,
                priority=Priority(app.priority).name,
                is_duplicate_flag=app.is_duplicate_flag,
                applicant_name=applicant_name,
                created_at=app.created_at,
            )
        )

    return result
