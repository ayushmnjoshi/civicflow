"""Integration tests for OCR engine, validation service, and the full upload flow.

Architecture
------------
- Most tests use a FakeOcrEngine that returns canned text (no Tesseract needed).
- ONE real-Tesseract test is marked with @pytest.mark.skipif.
- The db_session + client fixtures mirror test_applications.py / test_documents.py.
- get_storage is overridden with InMemoryStorage.
- get_ocr_engine is overridden with FakeOcrEngine.
"""

from __future__ import annotations

import io
import shutil
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.auth.security import create_access_token, hash_password
from app.config import settings
from app.constants import (
    VALIDATION_STATUS_ERROR,
    VALIDATION_STATUS_INVALID,
    VALIDATION_STATUS_PENDING,
    VALIDATION_STATUS_VALID,
    Priority,
)
from app.database import engine
from app.dependencies import get_db
from app.main import app
from app.models.ai_analysis import AIAnalysis
from app.models.application import Application, ApplicationStatus
from app.models.application_status_history import ApplicationStatusHistory
from app.models.department import Department
from app.models.document import Document
from app.models.role import Role
from app.models.user import User
from app.ocr.engine import AbstractOcrEngine, OcrError, OcrResult, get_ocr_engine
from app.services.storage_service import InMemoryStorage, get_storage


# ══════════════════════════════════════════════════════════════════════════════
# Fake OCR engine
# ══════════════════════════════════════════════════════════════════════════════

class FakeOcrEngine(AbstractOcrEngine):
    """Returns canned text without calling Tesseract."""

    def __init__(self, text: str = "", method: str = "tesseract") -> None:
        self._text = text
        self._method = method

    def extract(self, data: bytes, ext: str) -> OcrResult:
        return OcrResult(text=self._text, method=self._method)


class FailingOcrEngine(AbstractOcrEngine):
    """Always raises OcrError — simulates a corrupted file."""

    def extract(self, data: bytes, ext: str) -> OcrResult:
        raise OcrError("Simulated OCR failure: corrupt file.")


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


def _make_client(db_session, fake_storage, fake_ocr):
    def _db():
        yield db_session

    def _storage():
        return fake_storage

    def _ocr():
        return fake_ocr

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_storage] = _storage
    app.dependency_overrides[get_ocr_engine] = _ocr
    c = TestClient(app, raise_server_exceptions=False)
    return c


# ── Seed helpers ──────────────────────────────────────────────────────────────

def _role_id(db: Session, name: str) -> int:
    r = db.query(Role).filter(Role.name == name).first()
    assert r is not None, f"Role '{name}' not found"
    return r.id


def _make_user(db: Session, role: str, email: str, full_name: str = "Test User") -> User:
    u = User(
        email=email,
        password_hash=hash_password("Test@1234"),
        role_id=_role_id(db, role),
        full_name=full_name,
    )
    db.add(u)
    db.flush()
    return u


def _token(user: User) -> dict:
    tok = create_access_token(
        user_id=user.id,
        secret=settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
        expire_minutes=settings.access_token_expire_minutes,
    )
    return {"Authorization": f"Bearer {tok}"}


def _get_dept(db: Session) -> Department:
    d = db.query(Department).first()
    assert d is not None, "No departments seeded"
    return d


def _make_app(db: Session, citizen: User, dept: Department, status=ApplicationStatus.DOCUMENT_PENDING) -> Application:
    a = Application(
        citizen_id=citizen.id,
        department_id=dept.id,
        service_type="civic_services",
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


def _upload(client, user, app_id, doc_type="ID_PROOF", content=None, filename="test.pdf"):
    if content is None:
        content = _pdf_bytes()
    return client.post(
        f"/applications/{app_id}/documents",
        headers=_token(user),
        data={"doc_type": doc_type},
        files={"file": (filename, io.BytesIO(content), "application/octet-stream")},
    )


# ══════════════════════════════════════════════════════════════════════════════
# Part 1 — Safe failures: corrupt image still returns 201
# ══════════════════════════════════════════════════════════════════════════════

class TestSafeFailure:

    def test_corrupt_image_still_returns_201(self, db_session, fake_storage):
        """Valid PNG magic bytes but corrupt content → upload 201, doc ERROR, no crash."""
        ocr = FailingOcrEngine()
        client = _make_client(db_session, fake_storage, ocr)

        try:
            citizen = _make_user(db_session, "citizen", "corrupt_test@test.dev")
            dept = _get_dept(db_session)
            a = _make_app(db_session, citizen, dept)

            # Valid PNG magic bytes but garbage body
            corrupt_png = _png_bytes()[:8] + b"\x00corrupt data\xff\xfe"
            r = _upload(client, citizen, a.id, doc_type="ID_PROOF",
                        content=corrupt_png, filename="corrupt.png")

            assert r.status_code == 201, f"Expected 201, got {r.status_code}: {r.text}"

            # Document row exists with ERROR status
            doc = db_session.query(Document).filter(
                Document.application_id == a.id
            ).first()
            assert doc is not None
            assert doc.validation_status == VALIDATION_STATUS_ERROR

            # ai_analysis row must exist
            analysis = db_session.query(AIAnalysis).filter(
                AIAnalysis.document_id == doc.id
            ).first()
            assert analysis is not None
            result = analysis.validation_result
            assert result["result"] == "ERROR"
        finally:
            app.dependency_overrides.clear()


# ══════════════════════════════════════════════════════════════════════════════
# Part 2 — Process document (fake OCR)
# ══════════════════════════════════════════════════════════════════════════════

class TestProcessDocument:

    def _client_upload(self, db_session, fake_storage, ocr_text, app_id, citizen, doc_type="ID_PROOF"):
        ocr = FakeOcrEngine(text=ocr_text)
        client = _make_client(db_session, fake_storage, ocr)
        try:
            return _upload(client, citizen, app_id, doc_type=doc_type)
        finally:
            app.dependency_overrides.clear()

    def test_valid_ocr_sets_valid_status(self, db_session, fake_storage):
        citizen = _make_user(db_session, "citizen", "pd_valid@test.dev", "Ayush Joshi")
        dept = _get_dept(db_session)
        a = _make_app(db_session, citizen, dept)

        good_text = "Name: Ayush Joshi\nID Number: 1234 5678 9012\nResident card."
        r = self._client_upload(db_session, fake_storage, good_text, a.id, citizen)
        assert r.status_code == 201

        doc = db_session.query(Document).filter(Document.application_id == a.id).first()
        assert doc.validation_status == VALIDATION_STATUS_VALID

        analysis = db_session.query(AIAnalysis).filter(
            AIAnalysis.document_id == doc.id
        ).first()
        assert analysis is not None
        assert analysis.validation_result["result"] == "VALID"

    def test_invalid_ocr_sets_invalid_status(self, db_session, fake_storage):
        citizen = _make_user(db_session, "citizen", "pd_inv@test.dev", "Ayush Joshi")
        dept = _get_dept(db_session)
        a = _make_app(db_session, citizen, dept)

        bad_text = "Unrelated content, no name, no id."
        r = self._client_upload(db_session, fake_storage, bad_text, a.id, citizen)
        assert r.status_code == 201

        doc = db_session.query(Document).filter(Document.application_id == a.id).first()
        assert doc.validation_status == VALIDATION_STATUS_INVALID

    def test_rerunning_ocr_keeps_one_ai_analysis_row(self, db_session, fake_storage):
        """Re-running OCR on the same document must update, not insert a second row."""
        from app.services.validation_service import process_document

        citizen = _make_user(db_session, "citizen", "pd_rerun@test.dev", "Ayush Joshi")
        dept = _get_dept(db_session)
        a = _make_app(db_session, citizen, dept)

        # First run
        ocr1 = FakeOcrEngine(text="Name: Ayush Joshi ID: 1234 5678 9012")
        r = _upload(
            _make_client(db_session, fake_storage, ocr1), citizen, a.id
        )
        app.dependency_overrides.clear()
        assert r.status_code == 201
        doc = db_session.query(Document).filter(Document.application_id == a.id).first()

        # Second run via service directly
        ocr2 = FakeOcrEngine(text="updated text Ayush Joshi 1234 5678 9012")
        process_document(db_session, doc, fake_storage, ocr2)

        count = db_session.query(AIAnalysis).filter(
            AIAnalysis.document_id == doc.id
        ).count()
        assert count == 1, f"Expected 1 ai_analysis row, got {count}"


# ══════════════════════════════════════════════════════════════════════════════
# Part 3 — Flow tests (status transitions)
# ══════════════════════════════════════════════════════════════════════════════

class TestApplicationFlow:

    def _upload_doc(self, db_session, fake_storage, citizen, app_id, doc_type, ocr_text):
        ocr = FakeOcrEngine(text=ocr_text)
        client = _make_client(db_session, fake_storage, ocr)
        try:
            return _upload(client, citizen, app_id, doc_type=doc_type)
        finally:
            app.dependency_overrides.clear()

    def _history(self, db_session, app_id):
        return (
            db_session.query(ApplicationStatusHistory)
            .filter(ApplicationStatusHistory.application_id == app_id)
            .order_by(ApplicationStatusHistory.changed_at.asc())
            .all()
        )

    def test_partial_upload_stays_document_pending(self, db_session, fake_storage):
        """Only one required doc uploaded → status stays DOCUMENT_PENDING."""
        citizen = _make_user(db_session, "citizen", "flow_partial@test.dev", "Ayush Joshi")
        dept = _get_dept(db_session)
        a = _make_app(db_session, citizen, dept)

        # civic_services requires ID_PROOF + ADDRESS_PROOF — only upload ID_PROOF
        good_id = "Name: Ayush Joshi\nID Number: 1234 5678 9012"
        r = self._upload_doc(db_session, fake_storage, citizen, a.id, "ID_PROOF", good_id)
        assert r.status_code == 201

        db_session.refresh(a)
        assert a.status == ApplicationStatus.DOCUMENT_PENDING

    def test_all_valid_docs_end_at_validated(self, db_session, fake_storage):
        """All required docs valid → DOCUMENT_PENDING → VALIDATING → VALIDATED."""
        citizen = _make_user(db_session, "citizen", "flow_validated@test.dev", "Ayush Joshi")
        dept = _get_dept(db_session)
        a = _make_app(db_session, citizen, dept)

        good_id = "Name: Ayush Joshi\nID Number: 1234 5678 9012"
        good_addr = "Name: Ayush Joshi\nAddress: 123 Main St"

        r1 = self._upload_doc(db_session, fake_storage, citizen, a.id, "ID_PROOF", good_id)
        assert r1.status_code == 201

        r2 = self._upload_doc(db_session, fake_storage, citizen, a.id, "ADDRESS_PROOF", good_addr)
        assert r2.status_code == 201

        db_session.refresh(a)
        # Auto-assignment may run immediately: accept VALIDATED or ASSIGNED
        assert a.status in (ApplicationStatus.VALIDATED, ApplicationStatus.ASSIGNED)

        # History: DOCUMENT_PENDING → VALIDATING → VALIDATED must always be present
        hist = self._history(db_session, a.id)
        to_statuses = [h.to_status for h in hist]
        assert "VALIDATING" in to_statuses
        assert "VALIDATED" in to_statuses

    def test_invalid_doc_ends_at_additional_doc_required(self, db_session, fake_storage):
        """Last required doc is INVALID → ADDITIONAL_DOCUMENT_REQUIRED with reason codes."""
        citizen = _make_user(db_session, "citizen", "flow_adr@test.dev", "Ayush Joshi")
        dept = _get_dept(db_session)
        a = _make_app(db_session, citizen, dept)

        good_id = "Name: Ayush Joshi\nID Number: 1234 5678 9012"
        bad_addr = "short"  # too short → INVALID

        r1 = self._upload_doc(db_session, fake_storage, citizen, a.id, "ID_PROOF", good_id)
        assert r1.status_code == 201

        r2 = self._upload_doc(db_session, fake_storage, citizen, a.id, "ADDRESS_PROOF", bad_addr)
        assert r2.status_code == 201

        db_session.refresh(a)
        assert a.status == ApplicationStatus.ADDITIONAL_DOCUMENT_REQUIRED

        # Remarks must contain reason codes only
        hist = self._history(db_session, a.id)
        adr_hist = [h for h in hist if h.to_status == "ADDITIONAL_DOCUMENT_REQUIRED"]
        assert adr_hist, "No history row for ADDITIONAL_DOCUMENT_REQUIRED"
        remarks = adr_hist[-1].remarks or ""
        assert "invalid" in remarks.lower() or "missing" in remarks.lower()
        # Must not contain raw OCR text
        assert "short" not in remarks

    def test_system_user_is_changed_by_on_automatic_steps(self, db_session, fake_storage):
        """VALIDATING and VALIDATED history rows must use the system user."""
        from app.services.system_actor import get_system_user_id
        system_id = get_system_user_id(db_session)

        citizen = _make_user(db_session, "citizen", "flow_sysuser@test.dev", "Ayush Joshi")
        dept = _get_dept(db_session)
        a = _make_app(db_session, citizen, dept)

        good_id = "Name: Ayush Joshi\nID Number: 1234 5678 9012"
        good_addr = "Name: Ayush Joshi\nAddress: 123 Main St"

        self._upload_doc(db_session, fake_storage, citizen, a.id, "ID_PROOF", good_id)
        self._upload_doc(db_session, fake_storage, citizen, a.id, "ADDRESS_PROOF", good_addr)

        hist = self._history(db_session, a.id)
        auto_steps = [
            h for h in hist if h.to_status in ("VALIDATING", "VALIDATED", "ADDITIONAL_DOCUMENT_REQUIRED")
        ]
        for step in auto_steps:
            assert step.changed_by == system_id, (
                f"Step to {step.to_status} had changed_by={step.changed_by}, expected system={system_id}"
            )


# ══════════════════════════════════════════════════════════════════════════════
# Part 4 — Internal endpoints
# ══════════════════════════════════════════════════════════════════════════════

SERVICE_TOKEN = settings.internal_service_token


@pytest.fixture(scope="function")
def internal_client(db_session, fake_storage):
    ocr = FakeOcrEngine(text="Name: Test User ID: 1234 5678 9012")

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


class TestInternalEndpoints:

    def test_no_token_returns_401_ocr(self, internal_client, db_session, fake_storage):
        r = internal_client.post("/internal/ai/ocr", json={"document_id": 1})
        assert r.status_code == 401

    def test_wrong_token_returns_401_ocr(self, internal_client):
        r = internal_client.post(
            "/internal/ai/ocr",
            json={"document_id": 1},
            headers={"X-Service-Token": "wrong-token"},
        )
        assert r.status_code == 401

    def test_citizen_jwt_returns_401_on_internal(self, internal_client, db_session):
        citizen = _make_user(db_session, "citizen", "int_jwt@test.dev")
        r = internal_client.post(
            "/internal/ai/ocr",
            json={"document_id": 1},
            headers=_token(citizen),  # JWT Bearer token, NOT service token
        )
        assert r.status_code == 401

    def test_correct_token_unknown_doc_returns_404(self, internal_client):
        r = internal_client.post(
            "/internal/ai/ocr",
            json={"document_id": 999999},
            headers={"X-Service-Token": SERVICE_TOKEN},
        )
        assert r.status_code == 404

    def test_correct_token_unknown_app_returns_404_validate(self, internal_client):
        r = internal_client.post(
            "/internal/ai/validate",
            json={"application_id": 999999},
            headers={"X-Service-Token": SERVICE_TOKEN},
        )
        assert r.status_code == 404

    def test_correct_token_ocr_returns_200(self, internal_client, db_session, fake_storage):
        citizen = _make_user(db_session, "citizen", "int_ocr_ok@test.dev", "Test User")
        dept = _get_dept(db_session)
        a = _make_app(db_session, citizen, dept)

        # Upload a document first (using the internal_client fixture's overrides)
        r_up = _upload(internal_client, citizen, a.id, doc_type="ID_PROOF")
        assert r_up.status_code == 201
        doc_id = r_up.json()["document_id"]

        r = internal_client.post(
            "/internal/ai/ocr",
            json={"document_id": doc_id},
            headers={"X-Service-Token": SERVICE_TOKEN},
        )
        assert r.status_code == 200
        body = r.json()
        assert "extracted_text" in body
        assert "extracted_fields" in body

    def test_correct_token_validate_returns_200_no_status_change(
        self, internal_client, db_session, fake_storage
    ):
        citizen = _make_user(db_session, "citizen", "int_val_ok@test.dev", "Test User")
        dept = _get_dept(db_session)
        a = _make_app(db_session, citizen, dept)

        # Upload one doc — app stays DOCUMENT_PENDING (needs two for civic_services)
        r_up = _upload(internal_client, citizen, a.id, doc_type="ID_PROOF")
        assert r_up.status_code == 201

        r = internal_client.post(
            "/internal/ai/validate",
            json={"application_id": a.id},
            headers={"X-Service-Token": SERVICE_TOKEN},
        )
        assert r.status_code == 200
        body = r.json()
        assert "validation_result" in body
        assert "missing_docs" in body

        # Status must not have changed (read-only)
        db_session.refresh(a)
        assert a.status == ApplicationStatus.DOCUMENT_PENDING

    def test_empty_configured_token_refuses_all(self, db_session, fake_storage, monkeypatch):
        """When INTERNAL_SERVICE_TOKEN is empty, all requests are refused."""
        from app import config as cfg_module
        monkeypatch.setattr(cfg_module.settings, "internal_service_token", "")

        def _db():
            yield db_session

        app.dependency_overrides[get_db] = _db
        try:
            with TestClient(app, raise_server_exceptions=False) as c:
                r = c.post(
                    "/internal/ai/ocr",
                    json={"document_id": 1},
                    headers={"X-Service-Token": "anything"},
                )
        finally:
            app.dependency_overrides.clear()

        assert r.status_code == 401


# ══════════════════════════════════════════════════════════════════════════════
# Part 5 — Real Tesseract test (skipped if not found)
# ══════════════════════════════════════════════════════════════════════════════

_TESS_AVAILABLE = shutil.which("tesseract") is not None or __import__("os").path.exists(
    settings.tesseract_cmd
)


@pytest.mark.skipif(
    not _TESS_AVAILABLE,
    reason="Tesseract not found — skipping real OCR test",
)
class TestRealTesseract:
    """Uses real Tesseract on a Pillow-generated image with clearly fake data.

    The image text is large, high-contrast, and uses obviously synthetic data.
    Does NOT commit any binary sample files.
    """

    def test_pillow_generated_image_extracts_text(self, db_session, fake_storage):
        """Generate a PNG with Pillow, run real Tesseract, check OCR output.

        The fake ID number 1234 5678 9012 and name "Test Citizen" are used.
        These are clearly synthetic/demo values.
        """
        from PIL import Image, ImageDraw, ImageFont
        from app.ocr.engine import TesseractOcrEngine

        # Create a white image with large black text
        img = Image.new("RGB", (800, 300), color="white")
        draw = ImageDraw.Draw(img)

        fake_lines = [
            "FAKE DEMO ID CARD - NOT A REAL DOCUMENT",
            "Name: Test Citizen",
            "ID: 1234 5678 9012",
            "Issued for testing only",
        ]
        y = 20
        for line in fake_lines:
            draw.text((20, y), line, fill="black")
            y += 50

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        png_bytes = buf.getvalue()

        engine = TesseractOcrEngine(
            tesseract_cmd=settings.tesseract_cmd,
            max_pages=3,
            timeout_seconds=30,
            min_text_chars=20,
        )
        result = engine.extract(png_bytes, "png")

        assert result.method == "tesseract"
        assert len(result.text.strip()) > 0
        # The word "Citizen" should appear (OCR may vary, but large font is reliable)
        assert "Citizen" in result.text or "citizen" in result.text.lower()
