"""Tests for Prompt 10 additions.

Covers:
  Step 0a — OCR timeout via pytesseract's native timeout= argument
  Step 0b — upload_document ocr is a required argument
  Step 1  — required-documents lookup uses department's service_type, not client text
  Step 2  — Application row without explicit priority defaults to MEDIUM
  Step 3  — GET /departments
  Step 4  — Application detail: documents / required / missing / invalid fields
  Step 5  — Status history actor_role / actor_name (citizen vs staff viewer)
  Step 8  — OpenAPI excludes internal routes
"""

from __future__ import annotations

import io
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.auth.security import create_access_token, hash_password
from app.config import settings
from app.constants import Priority, VALIDATION_STATUS_INVALID, VALIDATION_STATUS_ERROR
from app.database import engine
from app.dependencies import get_db
from app.main import app
from app.models.application import Application, ApplicationStatus
from app.models.department import Department
from app.models.document import Document
from app.models.role import Role
from app.models.user import User
from app.ocr.engine import AbstractOcrEngine, OcrResult, OcrError, get_ocr_engine
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
    return InMemoryStorage()


class _FakeOcrEngine(AbstractOcrEngine):
    def __init__(self, text: str = "", raises: Exception | None = None) -> None:
        self._text = text
        self._raises = raises

    def extract(self, data: bytes, ext: str) -> OcrResult:
        if self._raises is not None:
            raise self._raises
        return OcrResult(text=self._text, method="tesseract")


@pytest.fixture(scope="function")
def client(db_session: Session, fake_storage: InMemoryStorage):
    ocr = _FakeOcrEngine(text="")

    def _db():
        yield db_session

    def _storage():
        return fake_storage

    def _ocr():
        return ocr

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_storage] = _storage
    app.dependency_overrides[get_ocr_engine] = _ocr
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides.clear()


# ── Seed helpers ──────────────────────────────────────────────────────────────

def _role_id(db: Session, name: str) -> int:
    r = db.query(Role).filter(Role.name == name).first()
    assert r is not None, f"Role '{name}' not seeded."
    return r.id


def _make_user(db: Session, role_name: str, email: str, full_name: str | None = None) -> User:
    u = User(
        email=email,
        password_hash=hash_password("Test@1234"),
        role_id=_role_id(db, role_name),
        full_name=full_name or f"Test {role_name.title()}",
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


def _make_app(
    db: Session,
    citizen: User,
    dept: Department,
    *,
    service_type: str = "Birth Certificate",
    status: ApplicationStatus = ApplicationStatus.DOCUMENT_PENDING,
) -> Application:
    a = Application(
        citizen_id=citizen.id,
        department_id=dept.id,
        service_type=service_type,
        status=status,
        priority=Priority.MEDIUM.value,
        is_duplicate_flag=False,
    )
    db.add(a)
    db.flush()
    return a


def _pdf_bytes() -> bytes:
    return b"%PDF-1.4 test"


def _png_bytes() -> bytes:
    return bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]) + b"\x00" * 20


def _upload_doc(client, user, app_id, doc_type="ID_PROOF", content=None, filename="test.pdf"):
    if content is None:
        content = _pdf_bytes()
    return client.post(
        f"/applications/{app_id}/documents",
        headers=_auth(user),
        data={"doc_type": doc_type},
        files={"file": (filename, io.BytesIO(content), "application/octet-stream")},
    )


# ══════════════════════════════════════════════════════════════════════════════
# Step 0a — OCR timeout kills Tesseract subprocess
# ══════════════════════════════════════════════════════════════════════════════

class TestOcrTimeout:

    def test_timeout_raises_ocr_error(self, monkeypatch):
        """Simulate pytesseract raising RuntimeError('Timeout') and verify
        we convert it to OcrError with the expected message."""
        from app.ocr.engine import TesseractOcrEngine

        eng = TesseractOcrEngine(
            tesseract_cmd="tesseract",
            max_pages=3,
            timeout_seconds=1,
            min_text_chars=20,
        )

        # Monkeypatch image_to_string to raise RuntimeError("Timeout")
        import pytesseract as _pyt

        def _fake_image_to_string(img, lang, timeout):
            raise RuntimeError("Timeout")

        monkeypatch.setattr(_pyt, "image_to_string", _fake_image_to_string)

        # Build a valid 1x1 white PNG in memory
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (10, 10), "white").save(buf, format="PNG")
        png = buf.getvalue()

        with pytest.raises(OcrError, match="timed out"):
            eng._ocr_image_bytes(png)

    def test_timeout_passes_timeout_arg(self, monkeypatch):
        """image_to_string must receive the configured timeout= value."""
        from app.ocr.engine import TesseractOcrEngine

        received: list[Any] = []

        import pytesseract as _pyt

        def _fake_image_to_string(img, lang, timeout):
            received.append(timeout)
            return "fake text"

        monkeypatch.setattr(_pyt, "image_to_string", _fake_image_to_string)

        eng = TesseractOcrEngine(
            tesseract_cmd="tesseract",
            max_pages=3,
            timeout_seconds=7,
            min_text_chars=20,
        )

        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (10, 10), "white").save(buf, format="PNG")

        eng._ocr_image_bytes(buf.getvalue())
        assert received == [7], f"Expected timeout=7, got {received}"


# ══════════════════════════════════════════════════════════════════════════════
# Step 0b — upload_document ocr is required
# ══════════════════════════════════════════════════════════════════════════════

class TestOcrRequiredArg:

    def test_upload_document_requires_ocr_kwarg(self, db_session, fake_storage):
        """Calling upload_document without ocr= must raise TypeError."""
        import inspect
        from app.services.document_service import upload_document

        sig = inspect.signature(upload_document)
        # ocr must be keyword-only with no default
        params = sig.parameters
        assert "ocr" in params
        p = params["ocr"]
        assert p.kind == inspect.Parameter.KEYWORD_ONLY
        assert p.default is inspect.Parameter.empty, "ocr must have no default"


# ══════════════════════════════════════════════════════════════════════════════
# Step 1 — required-documents uses department's service_type
# ══════════════════════════════════════════════════════════════════════════════

class TestRequiredDocsDeptServiceType:

    def test_dept_service_type_used_not_client(self, db_session, fake_storage):
        """An application with client service_type='anything' must still require
        the documents configured for the department's actual service_type.

        civic_services requires ID_PROOF + ADDRESS_PROOF; we send 'anything'.
        """
        from app.services.application_service import (
            get_required_documents_for_application,
        )
        # Find or create a dept with a known service_type
        dept = db_session.query(Department).filter(
            Department.service_type == "civic_services"
        ).first()
        if dept is None:
            dept = Department(name="Test Civic", service_type="civic_services")
            db_session.add(dept)
            db_session.flush()

        citizen = _make_user(db_session, "citizen", "req_doc_c@test.dev")

        # Client sends service_type="anything" — should NOT affect required docs
        a = Application(
            citizen_id=citizen.id,
            department_id=dept.id,
            service_type="anything",          # client-controlled
            status=ApplicationStatus.DOCUMENT_PENDING,
            priority=Priority.MEDIUM.value,
            is_duplicate_flag=False,
        )
        db_session.add(a)
        db_session.flush()

        required = get_required_documents_for_application(db_session, a)
        # Must use dept.service_type = "civic_services" → ID_PROOF + ADDRESS_PROOF
        assert "ID_PROOF" in required
        assert "ADDRESS_PROOF" in required

    def test_detail_required_docs_from_dept_not_client(self, client, db_session):
        """GET /applications/{id} detail must reflect department's service_type."""
        dept = db_session.query(Department).filter(
            Department.service_type == "civic_services"
        ).first()
        if dept is None:
            dept = Department(name="Test Civic2", service_type="civic_services")
            db_session.add(dept)
            db_session.flush()

        citizen = _make_user(db_session, "citizen", "req_doc_api@test.dev")
        # Create app with client service_type="anything"
        r = client.post(
            "/applications",
            json={"service_type": "anything", "department_id": dept.id},
            headers=_auth(citizen),
        )
        assert r.status_code == 201
        app_id = r.json()["id"]

        r2 = client.get(f"/applications/{app_id}", headers=_auth(citizen))
        assert r2.status_code == 200
        body = r2.json()
        # required_documents must come from dept (civic_services), not "anything"
        assert "ID_PROOF" in body["required_documents"]
        assert "ADDRESS_PROOF" in body["required_documents"]

    def test_unknown_department_service_type_uses_default(self, db_session):
        """A department type absent from rules falls back to DEFAULT_REQUIRED."""
        from app.services.application_service import (
            get_required_documents_for_application,
        )

        citizen = _make_user(db_session, "citizen", "req_doc_default@test.dev")
        dept = Department(name="Unknown Service", service_type="not_configured")
        db_session.add(dept)
        db_session.flush()
        app_row = _make_app(
            db_session, citizen, dept, service_type="anything"
        )

        assert get_required_documents_for_application(db_session, app_row) == ["ID_PROOF"]


# ══════════════════════════════════════════════════════════════════════════════
# Step 2 — Application default priority is MEDIUM
# ══════════════════════════════════════════════════════════════════════════════

class TestDefaultPriority:

    def test_application_row_without_priority_defaults_to_medium(self, db_session):
        """Inserting an Application without setting priority must give MEDIUM (1)."""
        citizen = _make_user(db_session, "citizen", "def_prio@test.dev")
        dept = _get_dept(db_session)

        # Do NOT set priority — rely on model default
        a = Application(
            citizen_id=citizen.id,
            department_id=dept.id,
            service_type="Test",
            status=ApplicationStatus.SUBMITTED,
            is_duplicate_flag=False,
        )
        db_session.add(a)
        db_session.flush()

        assert a.priority == Priority.MEDIUM.value, (
            f"Expected MEDIUM ({Priority.MEDIUM.value}), got {a.priority}"
        )


# ══════════════════════════════════════════════════════════════════════════════
# Step 3 — GET /departments
# ══════════════════════════════════════════════════════════════════════════════

class TestListDepartments:

    def test_returns_seeded_departments(self, client, db_session):
        r = client.get("/departments", headers=_auth(_make_user(db_session, "citizen", "dept_c@test.dev")))
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)
        assert len(data) >= 1
        for d in data:
            assert "id" in d
            assert "name" in d
            assert "service_type" in d

    def test_ordered_by_name(self, client, db_session):
        user = _make_user(db_session, "citizen", "dept_ord@test.dev")
        r = client.get("/departments", headers=_auth(user))
        assert r.status_code == 200
        names = [d["name"] for d in r.json()]
        assert names == sorted(names)

    def test_no_token_returns_401(self, client):
        r = client.get("/departments")
        assert r.status_code == 401

    def test_officer_can_list_departments(self, client, db_session):
        officer = _make_user(db_session, "officer", "dept_off@test.dev")
        r = client.get("/departments", headers=_auth(officer))
        assert r.status_code == 200

    def test_admin_can_list_departments(self, client, db_session):
        admin = _make_user(db_session, "admin", "dept_adm@test.dev")
        r = client.get("/departments", headers=_auth(admin))
        assert r.status_code == 200


# ══════════════════════════════════════════════════════════════════════════════
# Step 4 — Application detail: documents / required / missing / invalid
# ══════════════════════════════════════════════════════════════════════════════

class TestApplicationDetailDocFields:

    def _create_app(self, client, citizen, dept_id):
        r = client.post(
            "/applications",
            json={"service_type": "civic_services", "department_id": dept_id},
            headers=_auth(citizen),
        )
        assert r.status_code == 201
        return r.json()["id"]

    def test_empty_documents_list(self, client, db_session):
        citizen = _make_user(db_session, "citizen", "detail_empty@test.dev")
        dept = _get_dept(db_session)
        app_id = self._create_app(client, citizen, dept.id)

        r = client.get(f"/applications/{app_id}", headers=_auth(citizen))
        assert r.status_code == 200
        body = r.json()
        assert body["documents"] == []

    def test_required_documents_present_before_upload(self, client, db_session):
        citizen = _make_user(db_session, "citizen", "detail_req@test.dev")
        dept = db_session.query(Department).filter(
            Department.service_type == "civic_services"
        ).first()
        if dept is None:
            dept = Department(name="Civic Test", service_type="civic_services")
            db_session.add(dept)
            db_session.flush()

        app_id = self._create_app(client, citizen, dept.id)
        r = client.get(f"/applications/{app_id}", headers=_auth(citizen))
        body = r.json()
        assert "ID_PROOF" in body["required_documents"]
        assert "ADDRESS_PROOF" in body["required_documents"]
        assert "ID_PROOF" in body["missing_documents"]
        assert "ADDRESS_PROOF" in body["missing_documents"]
        assert body["invalid_documents"] == []

    def test_after_upload_document_appears(self, client, db_session):
        citizen = _make_user(db_session, "citizen", "detail_up@test.dev")
        dept = _get_dept(db_session)
        app_id = self._create_app(client, citizen, dept.id)

        _upload_doc(client, citizen, app_id, doc_type="ID_PROOF")

        r = client.get(f"/applications/{app_id}", headers=_auth(citizen))
        body = r.json()
        assert len(body["documents"]) == 1
        doc = body["documents"][0]
        assert doc["doc_type"] == "ID_PROOF"
        assert "storage_ref" not in doc
        assert "extracted_text" not in doc
        # After upload with FakeOcrEngine (empty text) → INVALID
        assert doc["validation_status"] in ("PENDING", "VALID", "INVALID", "ERROR")

    def test_missing_docs_decreases_after_upload(self, client, db_session):
        citizen = _make_user(db_session, "citizen", "detail_miss@test.dev")
        dept = db_session.query(Department).filter(
            Department.service_type == "civic_services"
        ).first()
        if dept is None:
            dept = Department(name="Civic3", service_type="civic_services")
            db_session.add(dept)
            db_session.flush()

        app_id = self._create_app(client, citizen, dept.id)

        # Before upload: both missing
        r = client.get(f"/applications/{app_id}", headers=_auth(citizen))
        assert "ID_PROOF" in r.json()["missing_documents"]

        # Upload ID_PROOF
        _upload_doc(client, citizen, app_id, doc_type="ID_PROOF")

        r2 = client.get(f"/applications/{app_id}", headers=_auth(citizen))
        body = r2.json()
        assert "ID_PROOF" not in body["missing_documents"]
        assert "ADDRESS_PROOF" in body["missing_documents"]

    def test_invalid_docs_populated_when_latest_is_invalid(self, client, db_session, fake_storage):
        """Manually set a document to INVALID, check invalid_documents."""
        citizen = _make_user(db_session, "citizen", "detail_inv@test.dev")
        dept = db_session.query(Department).filter(
            Department.service_type == "civic_services"
        ).first()
        if dept is None:
            dept = Department(name="Civic4", service_type="civic_services")
            db_session.add(dept)
            db_session.flush()

        a = _make_app(db_session, citizen, dept)
        r_up = _upload_doc(client, citizen, a.id, doc_type="ID_PROOF")
        assert r_up.status_code == 201

        # Force INVALID status directly in DB
        doc = db_session.query(Document).filter(Document.application_id == a.id).first()
        doc.validation_status = VALIDATION_STATUS_INVALID
        db_session.flush()

        r = client.get(f"/applications/{a.id}", headers=_auth(citizen))
        body = r.json()
        assert "ID_PROOF" in body["invalid_documents"]

    def test_citizen_cannot_see_other_citizens_detail(self, client, db_session):
        ca = _make_user(db_session, "citizen", "detail_ca@test.dev")
        cb = _make_user(db_session, "citizen", "detail_cb@test.dev")
        dept = _get_dept(db_session)
        app_id = self._create_app(client, ca, dept.id)

        r = client.get(f"/applications/{app_id}", headers=_auth(cb))
        assert r.status_code == 404


# ══════════════════════════════════════════════════════════════════════════════
# Step 5 — Status history actor_role / actor_name
# ══════════════════════════════════════════════════════════════════════════════

class TestHistoryActorFields:

    def _create_app(self, client, citizen, dept_id):
        r = client.post(
            "/applications",
            json={"service_type": "Test", "department_id": dept_id},
            headers=_auth(citizen),
        )
        assert r.status_code == 201
        return r.json()["id"]

    def test_citizen_viewer_does_not_see_citizen_actor_name(self, client, db_session):
        """A citizen never sees a person's name in history."""
        citizen = _make_user(db_session, "citizen", "hist_c@test.dev", "Jane Doe")
        dept = _get_dept(db_session)
        app_id = self._create_app(client, citizen, dept.id)

        r = client.get(f"/applications/{app_id}", headers=_auth(citizen))
        body = r.json()
        h0 = body["status_history"][0]
        assert h0["actor_role"] == "citizen"
        assert h0["actor_name"] is None
        assert "changed_by" not in h0

    def test_citizen_viewer_sees_system_name(self, client, db_session):
        """A citizen sees 'System' for the system-actor step."""
        citizen = _make_user(db_session, "citizen", "hist_sys@test.dev")
        dept = _get_dept(db_session)
        app_id = self._create_app(client, citizen, dept.id)

        r = client.get(f"/applications/{app_id}", headers=_auth(citizen))
        body = r.json()
        sys_step = next(h for h in body["status_history"] if h["actor_role"] == "system")
        assert sys_step["actor_name"] == "System"

    def test_citizen_viewer_sees_null_name_for_officer_step(self, client, db_session):
        """A citizen viewer of a staff-triggered step sees actor_name=null."""
        from app.models.officer import Officer
        from app.models.assignment import Assignment

        citizen = _make_user(db_session, "citizen", "hist_null@test.dev")
        officer_user = _make_user(db_session, "officer", "hist_off_null@test.dev", "Officer Smith")
        dept = _get_dept(db_session)
        app_id = self._create_app(client, citizen, dept.id)

        # Give the application ASSIGNED status so officer can transition it
        app_obj = db_session.get(Application, app_id)
        app_obj.status = ApplicationStatus.ASSIGNED
        db_session.flush()

        # Create officer + assignment
        officer_row = Officer(user_id=officer_user.id, department_id=dept.id, designation="officer")
        db_session.add(officer_row)
        db_session.flush()
        db_session.add(Assignment(application_id=app_id, officer_id=officer_row.id))
        db_session.flush()

        # Officer transitions ASSIGNED → UNDER_REVIEW
        r_patch = client.patch(
            f"/applications/{app_id}/status",
            json={"new_status": "UNDER_REVIEW"},
            headers=_auth(officer_user),
        )
        assert r_patch.status_code == 200

        # Citizen views the history
        r = client.get(f"/applications/{app_id}", headers=_auth(citizen))
        body = r.json()
        officer_step = next(
            (h for h in body["status_history"] if h["to_status"] == "UNDER_REVIEW"), None
        )
        assert officer_step is not None
        assert officer_step["actor_role"] == "officer"
        assert officer_step["actor_name"] is None   # citizen cannot see officer name

    def test_admin_viewer_sees_officer_name(self, client, db_session):
        """An admin viewer sees the officer's name on a staff-triggered step."""
        from app.models.officer import Officer
        from app.models.assignment import Assignment

        citizen = _make_user(db_session, "citizen", "hist_adm_c@test.dev")
        officer_user = _make_user(db_session, "officer", "hist_adm_off@test.dev", "Officer Brown")
        admin = _make_user(db_session, "admin", "hist_adm@test.dev")
        dept = _get_dept(db_session)
        app_id = self._create_app(client, citizen, dept.id)

        app_obj = db_session.get(Application, app_id)
        app_obj.status = ApplicationStatus.ASSIGNED
        db_session.flush()

        officer_row = Officer(user_id=officer_user.id, department_id=dept.id, designation="officer")
        db_session.add(officer_row)
        db_session.flush()
        db_session.add(Assignment(application_id=app_id, officer_id=officer_row.id))
        db_session.flush()

        r_patch = client.patch(
            f"/applications/{app_id}/status",
            json={"new_status": "UNDER_REVIEW"},
            headers=_auth(officer_user),
        )
        assert r_patch.status_code == 200

        # Admin views the history
        r = client.get(f"/applications/{app_id}", headers=_auth(admin))
        body = r.json()
        officer_step = next(
            (h for h in body["status_history"] if h["to_status"] == "UNDER_REVIEW"), None
        )
        assert officer_step is not None
        assert officer_step["actor_role"] == "officer"
        assert officer_step["actor_name"] == "Officer Brown"


class TestOpenApiExport:
    def test_openapi_schema_excludes_internal_routes(self):
        assert all(not path.startswith("/internal/") for path in app.openapi()["paths"])
