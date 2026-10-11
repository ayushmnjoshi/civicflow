"""Officer model."""

from sqlalchemy import Column, ForeignKey, Integer, String
from .base import Base


class Officer(Base):
    __tablename__ = "officers"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    department_id = Column(Integer, ForeignKey("departments.id"), nullable=False)
    designation = Column(String, nullable=False)
