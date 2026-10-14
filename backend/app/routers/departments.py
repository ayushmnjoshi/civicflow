"""Departments router.

GET /departments — any authenticated user; returns [{id, name, service_type}] ordered by name.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
from app.dependencies import get_db
from app.models.department import Department
from app.models.user import User
from app.schemas.department import DepartmentResponse

router = APIRouter(prefix="/departments", tags=["departments"])


@router.get(
    "",
    response_model=list[DepartmentResponse],
    summary="List all departments (any authenticated user)",
)
def list_departments(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[DepartmentResponse]:
    """Return all departments ordered by name."""
    depts = (
        db.query(Department)
        .order_by(Department.name.asc())
        .all()
    )
    return [
        DepartmentResponse(id=d.id, name=d.name, service_type=d.service_type)
        for d in depts
    ]
