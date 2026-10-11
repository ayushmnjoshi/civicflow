"""AI analysis model — stores OCR/LLM results per document."""

from sqlalchemy import Column, DateTime, ForeignKey, Integer, Text
from sqlalchemy.dialects.postgresql import JSONB
from .base import Base


class AIAnalysis(Base):
    __tablename__ = "ai_analysis"

    id = Column(Integer, primary_key=True)
    document_id = Column(Integer, ForeignKey("documents.id"), nullable=False)
    extracted_text = Column(Text, nullable=True)
    extracted_fields = Column(JSONB, nullable=True)
    validation_result = Column(JSONB, nullable=True)
    processed_at = Column(DateTime(timezone=True), nullable=False)
