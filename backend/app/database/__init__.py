"""Database session factory and engine.

Reads DATABASE_URL from the Settings object (pydantic-settings, loaded from
.env).  Alembic's env.py still loads its own URL via python-dotenv directly
so the two paths remain independent.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings

engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,   # detect stale connections automatically
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
