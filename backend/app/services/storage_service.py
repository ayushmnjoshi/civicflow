"""Object storage service (MinIO backend).

Design decisions
----------------
- Bucket is created lazily on first put, not at import time.  If MinIO is
  down the app and /health still start normally; only upload/download fail.
- Object key: applications/{application_id}/{uuid4}.{ext}
  The user's filename is never part of the key.
- Exposed as a FastAPI dependency (``get_storage``) so tests can inject an
  in-memory fake with ``app.dependency_overrides``.
- A ``StorageError`` wraps every MinIO SDK exception so callers don't need to
  import the MinIO client library.
"""

from __future__ import annotations

import io
import logging
import uuid
from abc import ABC, abstractmethod
from collections.abc import Iterator
from typing import Any

logger = logging.getLogger(__name__)


# ── Exceptions ────────────────────────────────────────────────────────────────

class StorageError(Exception):
    """Raised when an object-storage operation fails."""


# ── Abstract interface ────────────────────────────────────────────────────────

class AbstractStorage(ABC):
    """Minimal interface used by document_service and the in-memory fake."""

    @abstractmethod
    def put(self, key: str, data: bytes, content_type: str) -> None:
        """Upload *data* under *key*."""

    @abstractmethod
    def get_stream(self, key: str) -> Iterator[bytes]:
        """Return an iterator of byte chunks for *key*."""

    @abstractmethod
    def delete(self, key: str) -> None:
        """Delete the object at *key*.  Best-effort — does not raise."""

    @staticmethod
    def make_key(application_id: int, ext: str) -> str:
        """Construct a deterministic, user-filename-free object key."""
        return f"applications/{application_id}/{uuid.uuid4()}.{ext}"


# ── MinIO implementation ──────────────────────────────────────────────────────

class MinIOStorage(AbstractStorage):
    """Storage backed by a real MinIO (or S3-compatible) server."""

    def __init__(
        self,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        secure: bool = False,
    ) -> None:
        from minio import Minio  # imported here so the module loads without minio

        self._client = Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=secure)
        self._bucket = bucket
        self._bucket_ensured = False

    def _ensure_bucket(self) -> None:
        """Create the bucket if it doesn't exist yet (lazy, called on first put)."""
        if self._bucket_ensured:
            return
        if not self._client.bucket_exists(self._bucket):
            self._client.make_bucket(self._bucket)
            logger.info("Created MinIO bucket '%s'.", self._bucket)
        self._bucket_ensured = True

    def put(self, key: str, data: bytes, content_type: str) -> None:
        try:
            self._ensure_bucket()
            self._client.put_object(
                self._bucket,
                key,
                io.BytesIO(data),
                length=len(data),
                content_type=content_type,
            )
        except Exception as exc:
            raise StorageError(f"MinIO put failed for key '{key}': {exc}") from exc

    def get_stream(self, key: str) -> Iterator[bytes]:
        try:
            resp = self._client.get_object(self._bucket, key)
            try:
                yield from resp
            finally:
                resp.close()
                resp.release_conn()
        except Exception as exc:
            raise StorageError(f"MinIO get failed for key '{key}': {exc}") from exc

    def delete(self, key: str) -> None:
        try:
            self._client.remove_object(self._bucket, key)
        except Exception as exc:
            # best-effort — log and swallow
            logger.error("MinIO delete failed for key '%s': %s", key, exc)


# ── In-memory fake (tests only) ───────────────────────────────────────────────

class InMemoryStorage(AbstractStorage):
    """Thread-safe in-memory storage for unit tests."""

    def __init__(self) -> None:
        self._store: dict[str, bytes] = {}

    def put(self, key: str, data: bytes, content_type: str) -> None:
        self._store[key] = data

    def get_stream(self, key: str) -> Iterator[bytes]:
        if key not in self._store:
            raise StorageError(f"Key not found: {key}")
        yield self._store[key]

    def delete(self, key: str) -> None:
        self._store.pop(key, None)

    def exists(self, key: str) -> bool:
        """Test helper — not part of the public interface."""
        return key in self._store

    def stat(self, key: str) -> dict[str, Any]:
        """Test helper — returns a dict with size info."""
        if key not in self._store:
            raise StorageError(f"Key not found: {key}")
        return {"size": len(self._store[key])}


# ── FastAPI dependency ────────────────────────────────────────────────────────

_storage: MinIOStorage | None = None


def _get_minio_storage() -> MinIOStorage:
    """Return the singleton MinIOStorage, creating it on first call."""
    global _storage
    if _storage is None:
        from app.config import settings

        _storage = MinIOStorage(
            endpoint=settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            bucket=settings.minio_bucket,
            secure=settings.minio_secure,
        )
    return _storage


def get_storage() -> AbstractStorage:
    """FastAPI dependency that yields the configured storage backend.

    Override with ``app.dependency_overrides[get_storage]`` in tests.
    """
    return _get_minio_storage()
