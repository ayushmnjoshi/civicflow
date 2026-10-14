"""Document service — business logic for upload and retrieval.

Upload order (spec):
  1. Validate file (type, magic bytes, size).
  2. PUT object in MinIO.
  3. INSERT document row.
  4. COMMIT.
  If DB step fails: delete the object (best-effort) and log.
  If MinIO step fails: no DB row, raise StorageError (caller returns 503).

GET access rules:
  - Owner citizen: always allowed.
  - Officer: only if they have an active assignment (unassigned_at IS NULL).
  - Admin: always allowed.
  - Everyone else: 403.
"""

from __future__ import annotations

import logging
import os
import re
import unicodedata
from collections.abc import Iterator

from sqlalchemy.orm import Session

from app.constants import VALIDATION_STATUS_PENDING
from app.models.application import Application, ApplicationStatus
from app.models.assignment import Assignment
from app.models.document import Document
from app.models.officer import Officer
from app.models.user import User
from app.services.storage_service import AbstractStorage, StorageError

logger = logging.getLogger(__name__)

# ── Allowed file types ────────────────────────────────────────────────────────

#: Supported extensions → (MIME type, magic byte prefix)
_ALLOWED: dict[str, tuple[str, bytes]] = {
    "pdf":  ("application/pdf",  b"%PDF-"),
    "jpg":  ("image/jpeg",       bytes([0xFF, 0xD8, 0xFF])),
    "jpeg": ("image/jpeg",       bytes([0xFF, 0xD8, 0xFF])),
    "png":  ("image/png",        bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])),
}

#: Map stored extension → Content-Type header value
CONTENT_TYPE_MAP: dict[str, str] = {
    "pdf":  "application/pdf",
    "jpg":  "image/jpeg",
    "jpeg": "image/jpeg",
    "png":  "image/png",
}

# Status values that allow a citizen to upload
_UPLOAD_ALLOWED_STATUSES: frozenset[ApplicationStatus] = frozenset({
    ApplicationStatus.DOCUMENT_PENDING,
    ApplicationStatus.ADDITIONAL_DOCUMENT_REQUIRED,
})

_MAX_FILENAME_LEN = 255
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")


# ── Filename sanitisation ─────────────────────────────────────────────────────

def sanitise_filename(raw: str) -> str:
    """Return only the base name with path separators and control chars removed.

    The result is safe to store in the DB (never used in object keys).
    """
    # Normalise unicode to NFC
    raw = unicodedata.normalize("NFC", raw)
    # Strip any path components (handles both / and \)
    basename = os.path.basename(raw.replace("\\", "/"))
    # Remove control characters
    basename = _CONTROL_CHAR_RE.sub("", basename)
    # Cap length
    return basename[:_MAX_FILENAME_LEN] or "unnamed"


# ── File validation ───────────────────────────────────────────────────────────

class FileValidationError(ValueError):
    """Raised when a file fails format or size validation."""


def validate_file(
    filename: str,
    data: bytes,
    max_bytes: int,
) -> tuple[str, str, str]:
    """Validate *data* and return ``(ext, mime_type, sanitised_filename)``.

    Checks (in order):
      1. Non-empty.
      2. Extension is in the allowed set.
      3. File size ≤ max_bytes (caller must have capped the read at max_bytes+1).
      4. Magic bytes match the declared extension.

    Raises FileValidationError on any failure.
    """
    if not data:
        raise FileValidationError("Empty file.")

    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in _ALLOWED:
        raise FileValidationError(
            f"File type '.{ext}' is not allowed. Allowed: pdf, jpg, jpeg, png."
        )

    if len(data) > max_bytes:
        raise FileValidationError(
            f"File exceeds the maximum size of {max_bytes // (1024 * 1024)} MB."
        )

    mime_type, magic = _ALLOWED[ext]
    if not data.startswith(magic):
        raise FileValidationError(
            f"File content does not match declared extension '.{ext}'."
        )

    safe_name = sanitise_filename(filename)
    return ext, mime_type, safe_name


# ── Upload ────────────────────────────────────────────────────────────────────

def upload_document(
    db: Session,
    storage: AbstractStorage,
    *,
    ocr,
    application_id: int,
    citizen_id: int,
    raw_filename: str,
    file_data: bytes,
    doc_type: str,
    max_upload_mb: int,
) -> Document:
    """Validate, store, and persist one document.

    Returns the newly created Document row.
    Raises:
      - FileValidationError  — caller should return 400
      - StorageError         — caller should return 503
      - LookupError          — application not found (caller returns 404)
      - PermissionError      — wrong owner or status (caller returns 400/403)
    """
    max_bytes = max_upload_mb * 1024 * 1024

    # ── 1. Validate file ──────────────────────────────────────────────────────
    ext, mime_type, safe_name = validate_file(raw_filename, file_data, max_bytes)

    # ── 2. Authorise ──────────────────────────────────────────────────────────
    app = db.get(Application, application_id)
    if app is None:
        raise LookupError(f"Application {application_id} not found.")
    if app.citizen_id != citizen_id:
        raise PermissionError("You do not own this application.")
    if app.status not in _UPLOAD_ALLOWED_STATUSES:
        raise FileValidationError(
            f"Documents cannot be uploaded when the application status is "
            f"'{app.status.value}'. "
            f"Allowed statuses: DOCUMENT_PENDING, ADDITIONAL_DOCUMENT_REQUIRED."
        )

    # ── 3. Build object key ───────────────────────────────────────────────────
    key = AbstractStorage.make_key(application_id, ext if ext != "jpeg" else "jpg")

    # ── 4. PUT in MinIO ───────────────────────────────────────────────────────
    # Raises StorageError if it fails — no DB row written.
    storage.put(key, file_data, mime_type)
    logger.info(
        "Uploaded object key=%s application=%d size=%d",
        key, application_id, len(file_data),
    )

    # ── 5. INSERT document row ────────────────────────────────────────────────
    doc = Document(
        application_id=application_id,
        doc_type=doc_type,
        storage_ref=key,
        original_filename=safe_name,
        validation_status=VALIDATION_STATUS_PENDING,
    )
    try:
        db.add(doc)
        db.commit()
        db.refresh(doc)
    except Exception as exc:
        db.rollback()
        # Best-effort delete the object we just uploaded
        try:
            storage.delete(key)
            logger.info("Cleaned up orphaned object key=%s after DB failure", key)
        except Exception as del_exc:
            logger.error(
                "Failed to clean up object key=%s after DB failure: %s", key, del_exc
            )
        raise exc

    # ── 6. Post-upload: OCR + advance (failures must never break 201) ─────────
    _run_post_upload_hooks(db, storage, doc, ocr)

    return doc



def _run_post_upload_hooks(
    db: Session,
    storage: AbstractStorage,
    document: Document,
    ocr,
) -> None:
    """Run OCR validation and attempt to advance the application.

    Called after a successful upload commit.
    Any exception here MUST NOT affect the 201 response — log and return.
    The caller never sees errors from this function.
    Never logs OCR text or document content.
    """
    from app.services.validation_service import advance_application, process_document

    try:
        process_document(db, document, storage, ocr)
    except Exception:
        logger.exception(
            "Post-upload: process_document failed doc_id=%d", document.id
        )

    try:
        advance_application(db, document.application_id)
    except Exception:
        logger.exception(
            "Post-upload: advance_application failed application_id=%d",
            document.application_id,
        )


# ── Access-check helper ───────────────────────────────────────────────────────

def can_access_document(
    db: Session,
    doc: Document,
    user: User,
    user_role: str,
) -> bool:
    """Return True if *user* may download *doc*."""
    if user_role == "admin":
        return True

    app = db.get(Application, doc.application_id)
    if app is None:
        return False

    if user_role == "citizen":
        return app.citizen_id == user.id

    if user_role == "officer":
        officer = (
            db.query(Officer)
            .filter(Officer.user_id == user.id)
            .first()
        )
        if officer is None:
            return False
        return (
            db.query(Assignment)
            .filter(
                Assignment.application_id == doc.application_id,
                Assignment.officer_id == officer.id,
                Assignment.unassigned_at.is_(None),
            )
            .first()
        ) is not None

    return False


# ── GET stream ────────────────────────────────────────────────────────────────

def stream_document(
    storage: AbstractStorage,
    key: str,
) -> Iterator[bytes]:
    """Yield chunks for the object at *key*.  Raises StorageError on failure."""
    return storage.get_stream(key)
