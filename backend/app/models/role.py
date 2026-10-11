"""Role model — citizen / officer / dept_head / admin."""

from sqlalchemy import Column, Integer, String
from .base import Base


class Role(Base):
    __tablename__ = "roles"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False, unique=True)
