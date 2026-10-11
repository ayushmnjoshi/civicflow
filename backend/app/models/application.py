"""Application model with status enum."""

import enum

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    func,
)
from .base import Base


class ApplicationStatus(str, enum.Enum):
    SUBMITTED = "SUBMITTED"
    DOCUMENT_PENDING = "DOCUMENT_PENDING"
    VALIDATING = "VALIDATING"
    VALIDATED = "VALIDATED"
    ADDITIONAL_DOCUMENT_REQUIRED = "ADDITIONAL_DOCUMENT_REQUIRED"
    DOCUMENT_UPLOADED = "DOCUMENT_UPLOADED"
    ASSIGNED = "ASSIGNED"
    UNDER_REVIEW = "UNDER_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class Application(Base):
    __tablename__ = "applications"

    id = Column(Integer, primary_key=True)
    citizen_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    department_id = Column(Integer, ForeignKey("departments.id"), nullable=False)
    service_type = Column(String, nullable=False)
    status = Column(
        Enum(ApplicationStatus, name="applicationstatus"),
        nullable=False,
        default=ApplicationStatus.SUBMITTED,
    )
    priority = Column(Integer, nullable=False, default=0)
    is_duplicate_flag = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
