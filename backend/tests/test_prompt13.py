"""Tests for Prompt 13: re-upload loop, DOCUMENT_UPLOADED->VALIDATING edge, analysis endpoint."""

from __future__ import annotations

import io
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.auth.security import create_access_token, hash_password
from app.config import settings
from app.constants import (
    VALIDATION_STATUS_INVALID,
    VALIDATION_STATUS_VALID,
    Priority,
)
from app.database import engine
from app.dependencies import get_db
from app.main import app
from app.models.ai_analysis import AIAnalysis
from app.models.application import Application, ApplicationStatus
from app.models.application_status_history import ApplicationStatusHistory
from app.models.assignment import Assignment
from app.models.department import Department
from app.models.document import Document
from app.models.officer import Officer
from app.models.role import Role
from app.models.user import User
from app.ocr.engine import AbstractOcrEngine, OcrResult, get_ocr_engine
from app.services.state_machine import (
    InvalidTransitionError,
    can_transition,
    validate_transition,
)
from app.services.storage_service import InMemoryStorage, get_storage


# ══════════════════════════════════════════════════════════════════════════════
# Fake OCR engine
# ══════════════════════════════════════════════════════════════════════════════

class FakeOcrEngine(AbstractOcrEngine):
    def __init__(self, text: str = "") -> None:
        self._text = text

    def extract(self, data: bytes, ext: str) -> OcrResult:
        return OcrResult(text=self._text, method="fake")


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


def _make_client(db_session, fake_storage, ocr_text: str = ""):
    ocr = FakeOcrEngine(ocr_text)

    def _db():
        yield db_session

    def _st():
        return fake_storage

    def _ocr():
        return ocr

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_storage] = _st
    app.dependency_overrides[get_ocr_engine] = _ocr
    c = TestClient(app, raise_server_exceptions=False)
    return c


# ── Seed helpers ──────────────────────────────────────────────────────────────

def _uid() -> str:
    return uuid.uuid4().hex[:10]


def _role_id(db: Session, name: str) -> int:
    r = db.query(Role).filter(Role.name == name).first()
    assert r is not None
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


def _make_isolated_dept(db: Session, tag: str = "") -> Department:
    tag = tag or _uid()
    dept = Department(name=f"P13Dept {tag}", service_type="civic_services")
    db.add(dept)
    db.flush()
    return dept


def _make_officer(db: Session, dept_id: int, tag: str = "") -> tuple[User, Officer]:
    tag = tag or _uid()
    u = _make_user(db, "officer", f"off_{tag}@p13.test")
    o = Officer(user_id=u.id, department_id=dept_id, designation="Officer")
    db.add(o)
    db.flush()
    return u, o


def _make_app(
    db: Session,
    citizen_id: int,
    dept_id: int,
    status: ApplicationStatus = ApplicationStatus.DOCUMENT_PENDING,
) -> Application:
    a = Application(
        citizen_id=citizen_id,
        department_id=dept_id,
        service_type="civic_services",
        status=status,
        priority=Priority.MEDIUM.value,
        is_duplicate_flag=False,
    )
    db.add(a)
    db.flush()
    return a


def _assign(db: Session, app: Application, officer: Officer) -> Assignment:
    a = Assignment(application_id=app.id, officer_id=officer.id)
    db.add(a)
    db.flush()
    return a


def _pdf() -> bytes:
    return b"%PDF-1.4 test"


def _upload(client, user: User, app_id: int, doc_type: str, ocr_text: str = "") -> object:
    """Upload a PDF. (OCR text is baked into the FakeOcrEngine at client creation time.)"""
    return client.post(
        f"/applications/{app_id}/documents",
        headers=_token(user),
        data={"doc_type": doc_type},
        files={"file": ("test.pdf", io.BytesIO(_pdf()), "application/octet-stream")},
    )


def _history(db: Session, app_id: int) -> list[ApplicationStatusHistory]:
    return (
        db.query(ApplicationStatusHistory)
        .filter(ApplicationStatusHistory.application_id == app_id)
        .order_by(ApplicationStatusHistory.id.asc())
        .all()
    )


# ══════════════════════════════════════════════════════════════════════════════
# Part 1 — State machine: DOCUMENT_UPLOADED -> VALIDATING
# ══════════════════════════════════════════════════════════════════════════════

class TestStateMachineDUToValidating:

    def test_system_can_transition_DU_to_validating(self):
        assert can_transition("DOCUMENT_UPLOADED", "VALIDATING", "system") is True

    def test_citizen_cannot_DU_to_validating(self):
        assert can_transition("DOCUMENT_UPLOADED", "VALIDATING", "citizen") is False

    def test_officer_cannot_DU_to_validating(self):
        assert can_transition("DOCUMENT_UPLOADED", "VALIDATING", "officer") is False

    def test_DU_to_VALIDATED_still_illegal(self):
        """There is no direct DOCUMENT_UPLOADED -> VALIDATED edge."""
        assert can_transition("DOCUMENT_UPLOADED", "VALIDATED", "system") is False

    def test_DU_to_under_review_system_still_works(self):
        assert can_transition("DOCUMENT_UPLOADED", "UNDER_REVIEW", "system") is True

    def test_validate_transition_raises_for_citizen_DU_to_validating(self):
        with pytest.raises(InvalidTransitionError):
            validate_transition("DOCUMENT_UPLOADED", "VALIDATING", actor="citizen")

    def test_existing_edges_still_legal(self):
        """Spot-check that pre-existing edges are unaffected."""
        assert can_transition("DOCUMENT_PENDING", "VALIDATING", "system") is True
        assert can_transition("VALIDATING", "VALIDATED", "system") is True
        assert can_transition("ADDITIONAL_DOCUMENT_REQUIRED", "DOCUMENT_UPLOADED", "citizen") is True
        assert can_transition("UNDER_REVIEW", "APPROVED", "officer") is True

    def test_illegal_edges_still_illegal(self):
        assert can_transition("VALIDATED", "DOCUMENT_PENDING", "system") is False
        assert can_transition("APPROVED", "UNDER_REVIEW", "system") is False
        assert can_transition("DOCUMENT_UPLOADED", "APPROVED", "officer") is False


# ══════════════════════════════════════════════════════════════════════════════
# Part 2 — Re-upload: system path (no active assignment)
# ══════════════════════════════════════════════════════════════════════════════

class TestReUploadSystemPath:
    """No active assignment → system validation loop."""

    # Good OCR text satisfying both civic_services required docs
    GOOD_ID = "Name: Alice Smith\nID Number: 1234 5678 9012"
    GOOD_ADDR = "Name: Alice Smith\nAddress: 123 Main St"
    BAD_ADDR = "short"

    def test_valid_replacement_ends_at_validated_or_assigned(self, db_session, fake_storage):
        """System path: upload a good replacement → ADR->DU(citizen)->VALIDATING->VALIDATED(->ASSIGNED)."""
        # Start with ADDITIONAL_DOCUMENT_REQUIRED (no assignment)
        dept = _make_isolated_dept(db_session, "sp1")
        citizen = _make_user(db_session, "citizen", f"cit_sp1_{_uid()}@p13.test", "Alice Smith")
        application = _make_app(db_session, citizen.id, dept.id,
                                ApplicationStatus.ADDITIONAL_DOCUMENT_REQUIRED)

        # Pre-seed: good ID_PROOF already in DB
        doc_id_proof = Document(
            application_id=application.id,
            doc_type="ID_PROOF",
            storage_ref="fake/id.pdf",
            original_filename="id.pdf",
            validation_status=VALIDATION_STATUS_VALID,
        )
        db_session.add(doc_id_proof)
        db_session.flush()
        # ID_PROOF ai_analysis with valid result
        db_session.add(AIAnalysis(
            document_id=doc_id_proof.id,
            extracted_text=self.GOOD_ID,
            extracted_fields={"name_found": True},
            validation_result={"result": "VALID", "checks": [], "reasons": [], "ocr_method": "fake"},
            processed_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        ))
        db_session.flush()

        # Upload a good ADDRESS_PROOF (OCR engine returns good text)
        client = _make_client(db_session, fake_storage, self.GOOD_ADDR)
        r = _upload(client, citizen, application.id, "ADDRESS_PROOF")
        assert r.status_code == 201

        db_session.refresh(application)
        assert application.status in (ApplicationStatus.VALIDATED, ApplicationStatus.ASSIGNED)

        hist = _history(db_session, application.id)
        statuses = [(h.from_status, h.to_status) for h in hist]
        assert ("ADDITIONAL_DOCUMENT_REQUIRED", "DOCUMENT_UPLOADED") in statuses
        assert ("DOCUMENT_UPLOADED", "VALIDATING") in statuses
        assert ("VALIDATING", "VALIDATED") in statuses

    def test_invalid_replacement_stays_at_adr_with_reason_codes(self, db_session, fake_storage):
        """System path: upload an invalid replacement → ADR->DU->VALIDATING->ADR with codes."""
        dept = _make_isolated_dept(db_session, "sp2")
        citizen = _make_user(db_session, "citizen", f"cit_sp2_{_uid()}@p13.test", "Alice Smith")
        application = _make_app(db_session, citizen.id, dept.id,
                                ApplicationStatus.ADDITIONAL_DOCUMENT_REQUIRED)

        # Good ID_PROOF
        doc_id_proof = Document(
            application_id=application.id,
            doc_type="ID_PROOF",
            storage_ref="fake/id2.pdf",
            original_filename="id.pdf",
            validation_status=VALIDATION_STATUS_VALID,
        )
        db_session.add(doc_id_proof)
        db_session.flush()
        db_session.add(AIAnalysis(
            document_id=doc_id_proof.id,
            extracted_text=self.GOOD_ID,
            extracted_fields={"name_found": True},
            validation_result={"result": "VALID", "checks": [], "reasons": [], "ocr_method": "fake"},
            processed_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        ))
        db_session.flush()

        # Upload a BAD ADDRESS_PROOF
        client = _make_client(db_session, fake_storage, self.BAD_ADDR)
        r = _upload(client, citizen, application.id, "ADDRESS_PROOF")
        assert r.status_code == 201

        db_session.refresh(application)
        assert application.status == ApplicationStatus.ADDITIONAL_DOCUMENT_REQUIRED

        hist = _history(db_session, application.id)
        statuses = [(h.from_status, h.to_status) for h in hist]
        assert ("ADDITIONAL_DOCUMENT_REQUIRED", "DOCUMENT_UPLOADED") in statuses
        assert ("DOCUMENT_UPLOADED", "VALIDATING") in statuses
        assert ("VALIDATING", "ADDITIONAL_DOCUMENT_REQUIRED") in statuses

        # Last ADR history row must have reason codes in remarks
        adr_hist = [h for h in hist
                    if h.from_status == "VALIDATING" and h.to_status == "ADDITIONAL_DOCUMENT_REQUIRED"]
        assert adr_hist, "Expected VALIDATING->ADR history row"
        assert adr_hist[-1].remarks, "Remarks must not be empty"
        assert "invalid" in adr_hist[-1].remarks

    def test_missing_required_type_does_not_transition(self, db_session, fake_storage):
        """System path: upload only one of two required types → no transition."""
        dept = _make_isolated_dept(db_session, "sp3")
        citizen = _make_user(db_session, "citizen", f"cit_sp3_{_uid()}@p13.test", "Alice Smith")
        application = _make_app(db_session, citizen.id, dept.id,
                                ApplicationStatus.ADDITIONAL_DOCUMENT_REQUIRED)
        # Do NOT pre-seed ID_PROOF — only upload ADDRESS_PROOF

        client = _make_client(db_session, fake_storage, self.GOOD_ADDR)
        r = _upload(client, citizen, application.id, "ADDRESS_PROOF")
        assert r.status_code == 201

        db_session.refresh(application)
        # Status must remain ADDITIONAL_DOCUMENT_REQUIRED
        assert application.status == ApplicationStatus.ADDITIONAL_DOCUMENT_REQUIRED

    def test_re_upload_failure_does_not_break_201(self, db_session, fake_storage, monkeypatch):
        """If re_upload_advance raises, upload still returns 201."""
        from app.services import validation_service

        def _boom(*args, **kwargs):
            raise RuntimeError("simulated failure")

        monkeypatch.setattr(validation_service, "re_upload_advance", _boom)

        dept = _make_isolated_dept(db_session, "sp4")
        citizen = _make_user(db_session, "citizen", f"cit_sp4_{_uid()}@p13.test", "Alice Smith")
        application = _make_app(db_session, citizen.id, dept.id,
                                ApplicationStatus.ADDITIONAL_DOCUMENT_REQUIRED)

        client = _make_client(db_session, fake_storage, self.GOOD_ADDR)
        r = _upload(client, citizen, application.id, "ADDRESS_PROOF")
        assert r.status_code == 201

    def test_upload_in_document_pending_behaves_as_before(self, db_session, fake_storage):
        """advance_application (not re_upload_advance) fires for DOCUMENT_PENDING."""
        dept = _make_isolated_dept(db_session, "sp5")
        citizen = _make_user(db_session, "citizen", f"cit_sp5_{_uid()}@p13.test", "Alice Smith")
        application = _make_app(db_session, citizen.id, dept.id, ApplicationStatus.DOCUMENT_PENDING)

        client = _make_client(db_session, fake_storage, "random text")
        r = _upload(client, citizen, application.id, "ID_PROOF")
        assert r.status_code == 201
        # Status must still be DOCUMENT_PENDING (re_upload_advance is a no-op here)
        db_session.refresh(application)
        assert application.status == ApplicationStatus.DOCUMENT_PENDING

    def test_history_citizen_actor_on_DU_row(self, db_session, fake_storage):
        """The ADDITIONAL_DOCUMENT_REQUIRED -> DOCUMENT_UPLOADED row must have citizen as changed_by."""
        dept = _make_isolated_dept(db_session, "sp6")
        citizen = _make_user(db_session, "citizen", f"cit_sp6_{_uid()}@p13.test", "Alice Smith")
        application = _make_app(db_session, citizen.id, dept.id,
                                ApplicationStatus.ADDITIONAL_DOCUMENT_REQUIRED)

        # Pre-seed good ID_PROOF
        doc_id_proof = Document(
            application_id=application.id,
            doc_type="ID_PROOF",
            storage_ref="fake/id3.pdf",
            original_filename="id.pdf",
            validation_status=VALIDATION_STATUS_VALID,
        )
        db_session.add(doc_id_proof)
        db_session.flush()
        db_session.add(AIAnalysis(
            document_id=doc_id_proof.id,
            extracted_text=self.GOOD_ID,
            extracted_fields={"name_found": True},
            validation_result={"result": "VALID", "checks": [], "reasons": [], "ocr_method": "fake"},
            processed_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        ))
        db_session.flush()

        client = _make_client(db_session, fake_storage, self.GOOD_ADDR)
        _upload(client, citizen, application.id, "ADDRESS_PROOF")

        hist = _history(db_session, application.id)
        du_rows = [h for h in hist if h.to_status == "DOCUMENT_UPLOADED"]
        assert du_rows, "Expected a DOCUMENT_UPLOADED history row"
        assert du_rows[0].changed_by == citizen.id


# ══════════════════════════════════════════════════════════════════════════════
# Part 3 — Re-upload: officer path (active assignment)
# ══════════════════════════════════════════════════════════════════════════════

class TestReUploadOfficerPath:
    """Active assignment exists → DOCUMENT_UPLOADED -> UNDER_REVIEW (system)."""

    GOOD_ADDR = "Name: Bob Jones\nAddress: 456 Oak Ave"

    def test_officer_path_ends_at_under_review(self, db_session, fake_storage):
        """ADR + assignment: upload → ADR->DOCUMENT_UPLOADED(citizen)->UNDER_REVIEW(system)."""
        dept = _make_isolated_dept(db_session, "op1")
        citizen = _make_user(db_session, "citizen", f"cit_op1_{_uid()}@p13.test", "Bob Jones")
        officer_user, officer = _make_officer(db_session, dept.id, "op1")
        application = _make_app(db_session, citizen.id, dept.id,
                                ApplicationStatus.ADDITIONAL_DOCUMENT_REQUIRED)
        _assign(db_session, application, officer)

        client = _make_client(db_session, fake_storage, self.GOOD_ADDR)
        r = _upload(client, citizen, application.id, "ID_PROOF")
        assert r.status_code == 201

        db_session.refresh(application)
        assert application.status == ApplicationStatus.UNDER_REVIEW

        hist = _history(db_session, application.id)
        statuses = [(h.from_status, h.to_status) for h in hist]
        assert ("ADDITIONAL_DOCUMENT_REQUIRED", "DOCUMENT_UPLOADED") in statuses
        assert ("DOCUMENT_UPLOADED", "UNDER_REVIEW") in statuses

    def test_officer_path_assignment_unchanged(self, db_session, fake_storage):
        """The existing assignment must survive the re-upload."""
        dept = _make_isolated_dept(db_session, "op2")
        citizen = _make_user(db_session, "citizen", f"cit_op2_{_uid()}@p13.test", "Bob Jones")
        officer_user, officer = _make_officer(db_session, dept.id, "op2")
        application = _make_app(db_session, citizen.id, dept.id,
                                ApplicationStatus.ADDITIONAL_DOCUMENT_REQUIRED)
        asgn = _assign(db_session, application, officer)

        client = _make_client(db_session, fake_storage, self.GOOD_ADDR)
        _upload(client, citizen, application.id, "ID_PROOF")

        db_session.refresh(asgn)
        assert asgn.unassigned_at is None, "Assignment must not be closed"

    def test_officer_path_no_VALIDATING_in_history(self, db_session, fake_storage):
        """Officer path must NOT go through VALIDATING."""
        dept = _make_isolated_dept(db_session, "op3")
        citizen = _make_user(db_session, "citizen", f"cit_op3_{_uid()}@p13.test", "Bob Jones")
        officer_user, officer = _make_officer(db_session, dept.id, "op3")
        application = _make_app(db_session, citizen.id, dept.id,
                                ApplicationStatus.ADDITIONAL_DOCUMENT_REQUIRED)
        _assign(db_session, application, officer)

        client = _make_client(db_session, fake_storage, self.GOOD_ADDR)
        _upload(client, citizen, application.id, "ID_PROOF")

        hist = _history(db_session, application.id)
        validating_rows = [h for h in hist if h.to_status == "VALIDATING"]
        assert not validating_rows, "Officer path must not pass through VALIDATING"

    def test_officer_path_history_DU_row_has_citizen_as_actor(self, db_session, fake_storage):
        dept = _make_isolated_dept(db_session, "op4")
        citizen = _make_user(db_session, "citizen", f"cit_op4_{_uid()}@p13.test", "Bob Jones")
        officer_user, officer = _make_officer(db_session, dept.id, "op4")
        application = _make_app(db_session, citizen.id, dept.id,
                                ApplicationStatus.ADDITIONAL_DOCUMENT_REQUIRED)
        _assign(db_session, application, officer)

        client = _make_client(db_session, fake_storage, self.GOOD_ADDR)
        _upload(client, citizen, application.id, "ID_PROOF")

        hist = _history(db_session, application.id)
        du_rows = [h for h in hist if h.to_status == "DOCUMENT_UPLOADED"]
        assert du_rows
        assert du_rows[0].changed_by == citizen.id


# ══════════════════════════════════════════════════════════════════════════════
# Part 4 — GET /documents/{id}/analysis
# ══════════════════════════════════════════════════════════════════════════════

class TestDocumentAnalysis:

    def _setup(self, db_session: Session, fake_storage: InMemoryStorage):
        """Create citizen + doc + analysis, plus assigned officer."""
        dept = _make_isolated_dept(db_session, "an1")
        citizen = _make_user(db_session, "citizen", f"cit_an_{_uid()}@p13.test", "Test User")
        officer_user, officer = _make_officer(db_session, dept.id, "an1")

        application = _make_app(db_session, citizen.id, dept.id,
                                ApplicationStatus.UNDER_REVIEW)
        _assign(db_session, application, officer)

        doc = Document(
            application_id=application.id,
            doc_type="ID_PROOF",
            storage_ref="fake/analysis.pdf",
            original_filename="id.pdf",
            validation_status=VALIDATION_STATUS_VALID,
        )
        db_session.add(doc)
        db_session.flush()

        import datetime as dt
        analysis = AIAnalysis(
            document_id=doc.id,
            extracted_text="SHOULD NEVER APPEAR IN RESPONSE",
            extracted_fields={"name_found": True, "id_number": "****5678****"},
            validation_result={
                "result": "VALID",
                "checks": [{"field": "name", "pass": True}],
                "reasons": [],
                "ocr_method": "fake",
            },
            processed_at=dt.datetime.now(dt.timezone.utc),
        )
        db_session.add(analysis)
        db_session.flush()

        return doc, citizen, officer_user, application

    def test_assigned_officer_gets_200(self, db_session, fake_storage):
        doc, _, officer_user, _ = self._setup(db_session, fake_storage)
        client = _make_client(db_session, fake_storage)
        r = client.get(f"/documents/{doc.id}/analysis", headers=_token(officer_user))
        assert r.status_code == 200

    def test_response_shape(self, db_session, fake_storage):
        doc, _, officer_user, _ = self._setup(db_session, fake_storage)
        client = _make_client(db_session, fake_storage)
        r = client.get(f"/documents/{doc.id}/analysis", headers=_token(officer_user))
        body = r.json()
        assert body["document_id"] == doc.id
        assert body["doc_type"] == "ID_PROOF"
        assert body["validation_status"] == VALIDATION_STATUS_VALID
        assert "extracted_fields" in body
        assert "validation_result" in body
        assert "processed_at" in body

    def test_response_never_contains_extracted_text(self, db_session, fake_storage):
        doc, _, officer_user, _ = self._setup(db_session, fake_storage)
        client = _make_client(db_session, fake_storage)
        r = client.get(f"/documents/{doc.id}/analysis", headers=_token(officer_user))
        body_text = r.text
        assert "SHOULD NEVER APPEAR" not in body_text
        assert "extracted_text" not in body_text

    def test_owning_citizen_gets_403(self, db_session, fake_storage):
        doc, citizen, _, _ = self._setup(db_session, fake_storage)
        client = _make_client(db_session, fake_storage)
        r = client.get(f"/documents/{doc.id}/analysis", headers=_token(citizen))
        assert r.status_code == 403

    def test_other_citizen_gets_403(self, db_session, fake_storage):
        doc, _, _, _ = self._setup(db_session, fake_storage)
        other = _make_user(db_session, "citizen", f"other_{_uid()}@p13.test")
        client = _make_client(db_session, fake_storage)
        r = client.get(f"/documents/{doc.id}/analysis", headers=_token(other))
        assert r.status_code == 403

    def test_unassigned_officer_gets_403(self, db_session, fake_storage):
        doc, _, _, app = self._setup(db_session, fake_storage)
        dept = db_session.get(Department, app.department_id)
        other_officer_user, _ = _make_officer(db_session, dept.id, "una1")
        client = _make_client(db_session, fake_storage)
        r = client.get(f"/documents/{doc.id}/analysis", headers=_token(other_officer_user))
        assert r.status_code == 403

    def test_admin_gets_200(self, db_session, fake_storage):
        doc, _, _, _ = self._setup(db_session, fake_storage)
        admin = _make_user(db_session, "admin", f"adm_{_uid()}@p13.test")
        client = _make_client(db_session, fake_storage)
        r = client.get(f"/documents/{doc.id}/analysis", headers=_token(admin))
        assert r.status_code == 200

    def test_no_token_returns_401(self, db_session, fake_storage):
        doc, _, _, _ = self._setup(db_session, fake_storage)
        client = _make_client(db_session, fake_storage)
        r = client.get(f"/documents/{doc.id}/analysis")
        assert r.status_code == 401

    def test_unknown_document_id_returns_404(self, db_session, fake_storage):
        _, _, officer_user, _ = self._setup(db_session, fake_storage)
        client = _make_client(db_session, fake_storage)
        r = client.get("/documents/999999/analysis", headers=_token(officer_user))
        assert r.status_code == 404

    def test_no_analysis_row_returns_404(self, db_session, fake_storage):
        """A document with no ai_analysis row yet returns 404."""
        dept = _make_isolated_dept(db_session, "na1")
        citizen = _make_user(db_session, "citizen", f"cit_na_{_uid()}@p13.test")
        officer_user, officer = _make_officer(db_session, dept.id, "na2")
        application = _make_app(db_session, citizen.id, dept.id, ApplicationStatus.UNDER_REVIEW)
        _assign(db_session, application, officer)

        doc = Document(
            application_id=application.id,
            doc_type="ID_PROOF",
            storage_ref="fake/noanalysis.pdf",
            original_filename="id.pdf",
            validation_status="pending",
        )
        db_session.add(doc)
        db_session.flush()
        # No AIAnalysis row

        client = _make_client(db_session, fake_storage)
        r = client.get(f"/documents/{doc.id}/analysis", headers=_token(officer_user))
        assert r.status_code == 404

    def test_extracted_fields_are_masked(self, db_session, fake_storage):
        """The id_number in extracted_fields must be masked (stored masked in DB)."""
        doc, _, officer_user, _ = self._setup(db_session, fake_storage)
        client = _make_client(db_session, fake_storage)
        r = client.get(f"/documents/{doc.id}/analysis", headers=_token(officer_user))
        fields = r.json()["extracted_fields"]
        # The fixture stored a masked value — ensure it round-trips correctly
        assert "****" in fields.get("id_number", "****")
