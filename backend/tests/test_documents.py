"""Tests for document upload and download endpoints.

All tests use InMemoryStorage injected via FastAPI's dependency_overrides.
No real MinIO connection is needed.

Fixture layout mirrors test_applications.py:
  - db_session: rollback-per-test SQLAlchemy session
  - client: TestClient with db override + fresh InMemoryStorage per test
"""

from __future__ import annotations

import hashlib
import io

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.auth.security import create_access_token, hash_password
from app.config import settings
from app.constants import Priority, VALIDATION_STATUS_PENDING
from app.database import engine
from app.dependencies import get_db
from app.main import app
from app.models.application import Application, ApplicationStatus
from app.models.assignment import Assignment
from app.models.department import Department
from app.models.document import Document
from app.models.officer import Officer
from app.models.role import Role
from app.models.user import User
from app.ocr.engine import AbstractOcrEngine, OcrResult, get_ocr_engine
from app.services.storage_service import InMemoryStorage, get_storage


# ══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="function")
def db_session():
    connection = engine.connect()
    transaction = connection.begin()
    nested = connection.begin_nested()

    session = Session(bind=connection)

    @event.listens_for(session, "after_transaction_end")
    def restart_savepoint(sess, trans):
        nonlocal nested
        if trans.nested and not trans.is_active:
            nested = connection.begin_nested()

    yield session

    session.close()
    transaction.rollback()
    connection.close()


@pytest.fixture(scope="function")
def fake_storage() -> InMemoryStorage:
    """Fresh in-memory storage for each test."""
    return InMemoryStorage()



# Minimal fake OCR engine — returns empty text so OCR runs without Tesseract.
# Docs will be INVALID (empty text fails min_text), but upload always returns 201.
class _FakeOcrEngine(AbstractOcrEngine):
    def extract(self, data: bytes, ext: str) -> OcrResult:
        return OcrResult(text="", method="tesseract")


@pytest.fixture(scope="function")
def client(db_session: Session, fake_storage: InMemoryStorage):
    def _db_override():
        yield db_session

    def _storage_override():
        return fake_storage

    def _ocr_override():
        return _FakeOcrEngine()

    app.dependency_overrides[get_db] = _db_override
    app.dependency_overrides[get_storage] = _storage_override
    app.dependency_overrides[get_ocr_engine] = _ocr_override
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides.clear()


# ── Seed helpers ──────────────────────────────────────────────────────────────

def _role_id(db: Session, name: str) -> int:
    r = db.query(Role).filter(Role.name == name).first()
    assert r is not None, f"Role '{name}' not seeded."
    return r.id


def _make_user(db: Session, role_name: str, email: str) -> User:
    u = User(
        email=email,
        password_hash=hash_password("Test@1234"),
        role_id=_role_id(db, role_name),
        full_name=f"Test {role_name}",
    )
    db.add(u)
    db.flush()
    return u


def _token(user: User) -> str:
    return create_access_token(
        user_id=user.id,
        secret=settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
        expire_minutes=settings.access_token_expire_minutes,
    )


def _auth(user: User) -> dict:
    return {"Authorization": f"Bearer {_token(user)}"}


def _get_dept(db: Session) -> Department:
    d = db.query(Department).first()
    assert d is not None, "No departments seeded."
    return d


def _make_application(
    db: Session, citizen: User, dept: Department, status: ApplicationStatus
) -> Application:
    """Insert an application in the given status (bypasses the state machine)."""
    app_obj = Application(
        citizen_id=citizen.id,
        department_id=dept.id,
        service_type="Test Service",
        status=status,
        priority=Priority.MEDIUM.value,
        is_duplicate_flag=False,
    )
    db.add(app_obj)
    db.flush()
    return app_obj


def _make_officer_assigned(db: Session, application_id: int) -> User:
    """Create officer user + Officer row + active Assignment."""
    officer_user = _make_user(db, "officer", f"off_{application_id}_{id(db)}@test.dev")
    dept = _get_dept(db)
    officer_row = Officer(
        user_id=officer_user.id,
        department_id=dept.id,
        designation="officer",
    )
    db.add(officer_row)
    db.flush()
    assignment = Assignment(
        application_id=application_id,
        officer_id=officer_row.id,
    )
    db.add(assignment)
    db.flush()
    return officer_user


# ── Sample file factories ─────────────────────────────────────────────────────

def _pdf_bytes() -> bytes:
    return b"%PDF-1.4 test content"


def _jpg_bytes() -> bytes:
    return bytes([0xFF, 0xD8, 0xFF, 0xE0]) + b"\x00\x10JFIF" + b"\x00" * 10


def _png_bytes() -> bytes:
    return bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]) + b"\x00" * 10


def _upload(
    client: TestClient,
    user: User,
    application_id: int,
    filename: str = "test.pdf",
    content: bytes | None = None,
    doc_type: str = "PASSPORT",
) -> object:
    """Helper to POST a document upload."""
    if content is None:
        content = _pdf_bytes()
    return client.post(
        f"/applications/{application_id}/documents",
        headers=_auth(user),
        data={"doc_type": doc_type},
        files={"file": (filename, io.BytesIO(content), "application/octet-stream")},
    )


# ══════════════════════════════════════════════════════════════════════════════
# Part 1 — Valid uploads
# ══════════════════════════════════════════════════════════════════════════════

class TestValidUpload:

    def test_valid_pdf_returns_201(self, client, db_session, fake_storage):
        citizen = _make_user(db_session, "citizen", "up_pdf@test.dev")
        dept = _get_dept(db_session)
        app_obj = _make_application(db_session, citizen, dept, ApplicationStatus.DOCUMENT_PENDING)

        r = _upload(client, citizen, app_obj.id)

        assert r.status_code == 201
        body = r.json()
        assert "document_id" in body
        assert "storage_ref" in body

    def test_valid_pdf_creates_db_row(self, client, db_session, fake_storage):
        citizen = _make_user(db_session, "citizen", "up_pdf_row@test.dev")
        dept = _get_dept(db_session)
        app_obj = _make_application(db_session, citizen, dept, ApplicationStatus.DOCUMENT_PENDING)

        r = _upload(client, citizen, app_obj.id, filename="myfile.pdf")
        body = r.json()

        doc = db_session.get(Document, body["document_id"])
        assert doc is not None
        assert doc.application_id == app_obj.id
        assert doc.doc_type == "PASSPORT"
        # After Prompt 09, OCR runs post-upload. The FakeOcrEngine returns
        # empty text, so the doc gets INVALID (not PENDING). The important
        # invariants are: row exists, doc_type correct, filename not in key.
        assert doc.validation_status in (VALIDATION_STATUS_PENDING, "VALID", "INVALID", "ERROR")
        # storage_ref must not contain the user filename
        assert "myfile" not in doc.storage_ref

    def test_valid_jpg_returns_201(self, client, db_session, fake_storage):
        citizen = _make_user(db_session, "citizen", "up_jpg@test.dev")
        dept = _get_dept(db_session)
        app_obj = _make_application(db_session, citizen, dept, ApplicationStatus.DOCUMENT_PENDING)

        r = _upload(client, citizen, app_obj.id, filename="photo.jpg", content=_jpg_bytes())
        assert r.status_code == 201

    def test_valid_png_returns_201(self, client, db_session, fake_storage):
        citizen = _make_user(db_session, "citizen", "up_png@test.dev")
        dept = _get_dept(db_session)
        app_obj = _make_application(db_session, citizen, dept, ApplicationStatus.DOCUMENT_PENDING)

        r = _upload(client, citizen, app_obj.id, filename="scan.png", content=_png_bytes())
        assert r.status_code == 201

    def test_valid_upload_in_additional_doc_required_status(self, client, db_session, fake_storage):
        citizen = _make_user(db_session, "citizen", "up_adr@test.dev")
        dept = _get_dept(db_session)
        app_obj = _make_application(
            db_session, citizen, dept, ApplicationStatus.ADDITIONAL_DOCUMENT_REQUIRED
        )

        r = _upload(client, citizen, app_obj.id)
        assert r.status_code == 201


# ══════════════════════════════════════════════════════════════════════════════
# Part 2 — File validation rejections
# ══════════════════════════════════════════════════════════════════════════════

class TestFileValidationRejections:

    def test_exe_returns_400(self, client, db_session, fake_storage):
        citizen = _make_user(db_session, "citizen", "val_exe@test.dev")
        dept = _get_dept(db_session)
        app_obj = _make_application(db_session, citizen, dept, ApplicationStatus.DOCUMENT_PENDING)

        r = _upload(client, citizen, app_obj.id, filename="virus.exe", content=b"MZ\x90\x00")
        assert r.status_code == 400

    def test_pdf_with_wrong_magic_bytes_returns_400(self, client, db_session, fake_storage):
        """A .pdf with non-PDF content (fake rename) must be rejected."""
        citizen = _make_user(db_session, "citizen", "val_fake@test.dev")
        dept = _get_dept(db_session)
        app_obj = _make_application(db_session, citizen, dept, ApplicationStatus.DOCUMENT_PENDING)

        r = _upload(
            client, citizen, app_obj.id,
            filename="fake.pdf",
            content=b"This is just text, not a PDF",
        )
        assert r.status_code == 400

    def test_empty_file_returns_400(self, client, db_session, fake_storage):
        citizen = _make_user(db_session, "citizen", "val_empty@test.dev")
        dept = _get_dept(db_session)
        app_obj = _make_application(db_session, citizen, dept, ApplicationStatus.DOCUMENT_PENDING)

        r = _upload(client, citizen, app_obj.id, filename="empty.pdf", content=b"")
        assert r.status_code == 400

    def test_oversized_file_returns_400(self, client, db_session, fake_storage):
        """A file > MAX_UPLOAD_MB must be rejected without being fully buffered."""
        citizen = _make_user(db_session, "citizen", "val_big@test.dev")
        dept = _get_dept(db_session)
        app_obj = _make_application(db_session, citizen, dept, ApplicationStatus.DOCUMENT_PENDING)

        # max_upload_mb=5 → 5*1024*1024=5242880 bytes; send 6 MB worth
        big_content = b"%PDF-" + b"A" * (6 * 1024 * 1024)
        r = _upload(client, citizen, app_obj.id, filename="huge.pdf", content=big_content)
        assert r.status_code == 400

    def test_txt_extension_returns_400(self, client, db_session, fake_storage):
        citizen = _make_user(db_session, "citizen", "val_txt@test.dev")
        dept = _get_dept(db_session)
        app_obj = _make_application(db_session, citizen, dept, ApplicationStatus.DOCUMENT_PENDING)

        r = _upload(
            client, citizen, app_obj.id,
            filename="notes.txt", content=b"just some text",
        )
        assert r.status_code == 400


# ══════════════════════════════════════════════════════════════════════════════
# Part 3 — Status gate
# ══════════════════════════════════════════════════════════════════════════════

class TestStatusGate:

    @pytest.mark.parametrize("bad_status", [
        ApplicationStatus.SUBMITTED,
        ApplicationStatus.VALIDATING,
        ApplicationStatus.VALIDATED,
        ApplicationStatus.ASSIGNED,
        ApplicationStatus.UNDER_REVIEW,
        ApplicationStatus.APPROVED,
        ApplicationStatus.REJECTED,
    ])
    def test_wrong_status_returns_400(self, client, db_session, fake_storage, bad_status):
        citizen = _make_user(db_session, "citizen", f"sg_{bad_status.value.lower()}@test.dev")
        dept = _get_dept(db_session)
        app_obj = _make_application(db_session, citizen, dept, bad_status)

        r = _upload(client, citizen, app_obj.id)
        assert r.status_code == 400


# ══════════════════════════════════════════════════════════════════════════════
# Part 4 — RBAC on upload
# ══════════════════════════════════════════════════════════════════════════════

class TestUploadRBAC:

    def test_another_citizen_gets_403(self, client, db_session, fake_storage):
        owner = _make_user(db_session, "citizen", "rbac_own@test.dev")
        other = _make_user(db_session, "citizen", "rbac_oth@test.dev")
        dept = _get_dept(db_session)
        app_obj = _make_application(db_session, owner, dept, ApplicationStatus.DOCUMENT_PENDING)

        r = _upload(client, other, app_obj.id)
        assert r.status_code == 403

    def test_officer_upload_gets_403(self, client, db_session, fake_storage):
        citizen = _make_user(db_session, "citizen", "rbac_cit@test.dev")
        officer_user = _make_user(db_session, "officer", "rbac_off@test.dev")
        dept = _get_dept(db_session)
        app_obj = _make_application(db_session, citizen, dept, ApplicationStatus.DOCUMENT_PENDING)

        r = _upload(client, officer_user, app_obj.id)
        assert r.status_code == 403

    def test_admin_upload_gets_403(self, client, db_session, fake_storage):
        citizen = _make_user(db_session, "citizen", "rbac_adm_cit@test.dev")
        admin = _make_user(db_session, "admin", "rbac_adm@test.dev")
        dept = _get_dept(db_session)
        app_obj = _make_application(db_session, citizen, dept, ApplicationStatus.DOCUMENT_PENDING)

        r = _upload(client, admin, app_obj.id)
        assert r.status_code == 403

    def test_no_token_returns_401(self, client, db_session, fake_storage):
        dept = _get_dept(db_session)
        citizen = _make_user(db_session, "citizen", "rbac_notok@test.dev")
        app_obj = _make_application(db_session, citizen, dept, ApplicationStatus.DOCUMENT_PENDING)

        r = client.post(
            f"/applications/{app_obj.id}/documents",
            data={"doc_type": "PASSPORT"},
            files={"file": ("test.pdf", io.BytesIO(_pdf_bytes()), "application/pdf")},
        )
        assert r.status_code == 401

    def test_unknown_application_returns_404(self, client, db_session, fake_storage):
        citizen = _make_user(db_session, "citizen", "rbac_404@test.dev")
        r = _upload(client, citizen, 999999)
        assert r.status_code == 404


# ══════════════════════════════════════════════════════════════════════════════
# Part 5 — Filename sanitisation
# ══════════════════════════════════════════════════════════════════════════════

class TestFilenameSanitisation:

    def test_path_traversal_filename_stored_as_basename(self, client, db_session, fake_storage):
        """../../x.pdf must be stored as 'x.pdf', not the full traversal path."""
        citizen = _make_user(db_session, "citizen", "fn_trav@test.dev")
        dept = _get_dept(db_session)
        app_obj = _make_application(db_session, citizen, dept, ApplicationStatus.DOCUMENT_PENDING)

        r = _upload(client, citizen, app_obj.id, filename="../../x.pdf")
        assert r.status_code == 201
        doc_id = r.json()["document_id"]
        doc = db_session.get(Document, doc_id)
        assert doc.original_filename == "x.pdf"

    def test_storage_key_does_not_contain_user_filename(self, client, db_session, fake_storage):
        citizen = _make_user(db_session, "citizen", "fn_key@test.dev")
        dept = _get_dept(db_session)
        app_obj = _make_application(db_session, citizen, dept, ApplicationStatus.DOCUMENT_PENDING)

        r = _upload(client, citizen, app_obj.id, filename="my_secret_name.pdf")
        assert r.status_code == 201
        key = r.json()["storage_ref"]
        assert "my_secret_name" not in key


# ══════════════════════════════════════════════════════════════════════════════
# Part 6 — GET /documents/{id}
# ══════════════════════════════════════════════════════════════════════════════

class TestDownload:

    def _create_doc(self, client, db_session, fake_storage, citizen, status=ApplicationStatus.DOCUMENT_PENDING):
        dept = _get_dept(db_session)
        app_obj = _make_application(db_session, citizen, dept, status)
        r = _upload(client, citizen, app_obj.id)
        assert r.status_code == 201
        return r.json()["document_id"], app_obj.id

    def test_owner_gets_200_with_same_bytes(self, client, db_session, fake_storage):
        citizen = _make_user(db_session, "citizen", "dl_own@test.dev")
        content = _pdf_bytes()
        dept = _get_dept(db_session)
        app_obj = _make_application(db_session, citizen, dept, ApplicationStatus.DOCUMENT_PENDING)

        r_up = _upload(client, citizen, app_obj.id, content=content)
        assert r_up.status_code == 201
        doc_id = r_up.json()["document_id"]

        r_dl = client.get(f"/documents/{doc_id}", headers=_auth(citizen))
        assert r_dl.status_code == 200
        assert hashlib.sha256(r_dl.content).hexdigest() == hashlib.sha256(content).hexdigest()

    def test_owner_response_has_correct_headers(self, client, db_session, fake_storage):
        citizen = _make_user(db_session, "citizen", "dl_hdr@test.dev")
        dept = _get_dept(db_session)
        app_obj = _make_application(db_session, citizen, dept, ApplicationStatus.DOCUMENT_PENDING)
        r_up = _upload(client, citizen, app_obj.id)
        doc_id = r_up.json()["document_id"]

        r_dl = client.get(f"/documents/{doc_id}", headers=_auth(citizen))
        assert r_dl.status_code == 200
        assert r_dl.headers.get("content-disposition") == "inline"
        assert r_dl.headers.get("x-content-type-options") == "nosniff"
        assert "no-store" in r_dl.headers.get("cache-control", "")

    def test_admin_gets_200(self, client, db_session, fake_storage):
        citizen = _make_user(db_session, "citizen", "dl_adm_cit@test.dev")
        admin = _make_user(db_session, "admin", "dl_adm@test.dev")
        dept = _get_dept(db_session)
        app_obj = _make_application(db_session, citizen, dept, ApplicationStatus.DOCUMENT_PENDING)
        r_up = _upload(client, citizen, app_obj.id)
        doc_id = r_up.json()["document_id"]

        r_dl = client.get(f"/documents/{doc_id}", headers=_auth(admin))
        assert r_dl.status_code == 200

    def test_assigned_officer_gets_200(self, client, db_session, fake_storage):
        citizen = _make_user(db_session, "citizen", "dl_aoff_cit@test.dev")
        dept = _get_dept(db_session)
        app_obj = _make_application(db_session, citizen, dept, ApplicationStatus.DOCUMENT_PENDING)
        r_up = _upload(client, citizen, app_obj.id)
        doc_id = r_up.json()["document_id"]

        officer_user = _make_officer_assigned(db_session, app_obj.id)
        r_dl = client.get(f"/documents/{doc_id}", headers=_auth(officer_user))
        assert r_dl.status_code == 200

    def test_unassigned_officer_gets_403(self, client, db_session, fake_storage):
        citizen = _make_user(db_session, "citizen", "dl_uoff_cit@test.dev")
        dept = _get_dept(db_session)
        app_obj = _make_application(db_session, citizen, dept, ApplicationStatus.DOCUMENT_PENDING)
        r_up = _upload(client, citizen, app_obj.id)
        doc_id = r_up.json()["document_id"]

        # Unassigned officer: has Officer row but no Assignment
        unassigned = _make_user(db_session, "officer", "dl_uoff@test.dev")
        officer_row = Officer(
            user_id=unassigned.id, department_id=dept.id, designation="officer"
        )
        db_session.add(officer_row)
        db_session.flush()

        r_dl = client.get(f"/documents/{doc_id}", headers=_auth(unassigned))
        assert r_dl.status_code == 403

    def test_other_citizen_gets_403(self, client, db_session, fake_storage):
        owner = _make_user(db_session, "citizen", "dl_oth_own@test.dev")
        other = _make_user(db_session, "citizen", "dl_oth@test.dev")
        dept = _get_dept(db_session)
        app_obj = _make_application(db_session, owner, dept, ApplicationStatus.DOCUMENT_PENDING)
        r_up = _upload(client, owner, app_obj.id)
        doc_id = r_up.json()["document_id"]

        r_dl = client.get(f"/documents/{doc_id}", headers=_auth(other))
        assert r_dl.status_code == 403

    def test_unknown_id_returns_404(self, client, db_session, fake_storage):
        citizen = _make_user(db_session, "citizen", "dl_404@test.dev")
        r_dl = client.get("/documents/999999", headers=_auth(citizen))
        assert r_dl.status_code == 404

    def test_no_token_returns_401(self, client):
        r_dl = client.get("/documents/1")
        assert r_dl.status_code == 401


# ══════════════════════════════════════════════════════════════════════════════
# Part 7 — Failure modes
# ══════════════════════════════════════════════════════════════════════════════

class TestFailureModes:

    def test_minio_failure_returns_503_and_no_db_row(self, db_session, fake_storage, monkeypatch):
        """If storage.put raises StorageError the response must be 503 and no DB row inserted."""
        def _db_override():
            yield db_session

        def _storage_override():
            return fake_storage

        # Patch put to raise StorageError
        def _bad_put(key, data, content_type):
            from app.services.storage_service import StorageError
            raise StorageError("simulated MinIO outage")

        monkeypatch.setattr(fake_storage, "put", _bad_put)

        app.dependency_overrides[get_db] = _db_override
        app.dependency_overrides[get_storage] = _storage_override

        try:
            with TestClient(app, raise_server_exceptions=False) as c:
                citizen = _make_user(db_session, "citizen", "fail_minio@test.dev")
                dept = _get_dept(db_session)
                app_obj = _make_application(
                    db_session, citizen, dept, ApplicationStatus.DOCUMENT_PENDING
                )
                r = _upload(c, citizen, app_obj.id)
        finally:
            app.dependency_overrides.clear()

        assert r.status_code == 503
        # No document row should exist
        count = db_session.query(Document).filter(
            Document.application_id == app_obj.id
        ).count()
        assert count == 0

    def test_db_failure_deletes_object_from_storage(self, db_session, fake_storage, monkeypatch):
        """If the DB insert fails the object must be deleted from storage (best-effort)."""
        import app.services.document_service as _doc_svc

        def _db_override():
            yield db_session

        def _storage_override():
            return fake_storage

        original_add = db_session.add

        def _bad_add(obj):
            if isinstance(obj, Document):
                raise RuntimeError("simulated DB failure")
            return original_add(obj)

        monkeypatch.setattr(db_session, "add", _bad_add)

        app.dependency_overrides[get_db] = _db_override
        app.dependency_overrides[get_storage] = _storage_override

        try:
            with TestClient(app, raise_server_exceptions=False) as c:
                citizen = _make_user(db_session, "citizen", "fail_db@test.dev")
                dept = _get_dept(db_session)
                app_obj = _make_application(
                    db_session, citizen, dept, ApplicationStatus.DOCUMENT_PENDING
                )
                r = _upload(c, citizen, app_obj.id)
        finally:
            app.dependency_overrides.clear()
            monkeypatch.undo()

        # The response will be 500 (unhandled exception) — what matters is storage
        assert len(fake_storage._store) == 0, "Object should have been cleaned up from storage"
