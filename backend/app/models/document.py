"""Document model."""

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, func
from .base import Base


class Document(Base):
    __tablename__ = "documents"

    id = Column(Integer, primary_key=True)
    application_id = Column(Integer, ForeignKey("applications.id"), nullable=False)
    doc_type = Column(String, nullable=False)
    storage_ref = Column(String, nullable=False)
    original_filename = Column(String, nullable=False)
    validation_status = Column(String, nullable=False, default="pending")
    uploaded_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
