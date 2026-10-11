"""Alembic env.py — configured for CivicFlow.

Reads DATABASE_URL from environment (via .env) and wires in all
SQLAlchemy models so that ``alembic revision --autogenerate`` works.
"""

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool

from alembic import context

# ── path setup ────────────────────────────────────────────────────────────────
# Make `app` importable when alembic is run from the backend/ directory.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Load .env from the project root (two levels up from backend/)
_dotenv_path = Path(__file__).resolve().parents[3] / ".env"
if not _dotenv_path.exists():
    # Fallback: try civicflow root
    _dotenv_path = Path(__file__).resolve().parents[2] / ".env"
load_dotenv(dotenv_path=_dotenv_path, override=False)

# ── Alembic config ────────────────────────────────────────────────────────────
config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Override sqlalchemy.url from environment so we never store credentials in ini.
database_url = os.environ.get("DATABASE_URL")
if database_url:
    config.set_main_option("sqlalchemy.url", database_url)

# ── target metadata ───────────────────────────────────────────────────────────
# Import all models so their tables are registered on Base.metadata.
from app.models import Base  # noqa: E402  (import after sys.path tweak)

target_metadata = Base.metadata


# ── migration runners ─────────────────────────────────────────────────────────

def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (no live DB connection required)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (live DB connection)."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
