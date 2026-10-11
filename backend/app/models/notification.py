"""Notification model."""

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String
from .base import Base


class Notification(Base):
    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True)
    application_id = Column(Integer, ForeignKey("applications.id"), nullable=False)
    recipient_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    channel = Column(String, nullable=False)
    message = Column(String, nullable=False)
    status = Column(String, nullable=False, default="pending")  # pending/sent/failed
    sent_at = Column(DateTime(timezone=True), nullable=True)
