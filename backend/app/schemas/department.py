"""Pydantic schemas for the departments endpoints."""

from __future__ import annotations

from pydantic import BaseModel


class DepartmentResponse(BaseModel):
    """One department entry."""

    id: int
    name: str
    service_type: str
