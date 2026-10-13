"""Application settings — loaded from .env via pydantic-settings."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# The .env lives at civicflow/.env, which is two directories above this file
# (this file → app/ → backend/ → civicflow/).
_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",          # ignore unrecognised keys in .env
    )

    app_env: str = "development"
    secret_key: str = "change-me"
    database_url: str

    # ── JWT ──────────────────────────────────────────────────
    # Required — app refuses to start if missing from .env
    jwt_secret_key: str
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60

    # ── MinIO / object storage ────────────────────────────────
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_bucket: str = "civicflow"
    minio_secure: bool = False
    max_upload_mb: int = 5


# Single instance imported everywhere
settings = Settings()
