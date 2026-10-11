"""Escalation model."""

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String
from .base import Base


class Escalation(Base):
    __tablename__ = "escalations"

    id = Column(Integer, primary_key=True)
    application_id = Column(Integer, ForeignKey("applications.id"), nullable=False)
    escalated_to = Column(Integer, ForeignKey("users.id"), nullable=False)
    reason = Column(String, nullable=False)
    escalated_at = Column(DateTime(timezone=True), nullable=False)
    resolved_at = Column(DateTime(timezone=True), nullable=True)
