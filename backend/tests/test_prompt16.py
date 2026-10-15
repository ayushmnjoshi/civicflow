"""Tests for Prompt 16: audit logging.

Covers:
- audit_service: sanitizer, record() atomicity, no update/delete functions
- INVARIANT: status_change rows == history rows
- One test per hook: create, status_change, assignment, request-documents,
  upload, view, analysis_view, escalation_create, escalation_resolve,
  notification_send (including failed), reevaluate, register, login_success,
  login_failure (bad_password), login_failure (system_account), unknown_email.
- Atomicity: if commit raises, no audit row remains.
- Read-event resilience: denied view writes nothing; audit raise does not block download.
- No PII in details.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from app.auth.security import create_access_token, hash_password
from app.config import settings
from app.constants import AuditAction, AuditEntity, Priority
from app.database import engine
from app.dependencies import get_db
from app.main import app
from app.models.application import Application, ApplicationStatus
from app.models.application_status_history import ApplicationStatusHistory
from app.models.assignment import Assignment
from app.models.audit_log import AuditLog
from app.models.department import Department
from app.models.document import Document
from app.models.escalation import Escalation
from app.models.notification import Notification
from app.models.officer import Officer
from app.models.role import Role
from app.models.user import User
from app.ocr.engine import AbstractOcrEngine, OcrResult, get_ocr_engine
from app.services.audit_service import record, record_standalone, sanitize_details
from app.services.storage_service import InMemoryStorage, get_storage
from app.services.system_actor import get_system_user_id


# ══════════════════════════════════════════════════════════════════════════════
# Fake OCR engine (returns empty text so OCR runs without Tesseract)
# ══════════════════════════════════════════════════════════════════════════════

class _FakeOcrEngine(AbstractOcrEngine):
    def extract(self, data: bytes, ext: str) -> OcrResult:
        return OcrResult(text="", method="tesseract")


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
def fake_storage():
    return InMemoryStorage()


@pytest.fixture(scope="function")
def client(db_session, fake_storage):
    def _db():
        yield db_session

    def _storage():
        return fake_storage

    def _ocr():
        return _FakeOcrEngine()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_storage] = _storage
    app.dependency_overrides[get_ocr_engine] = _ocr
    c = TestClient(app, raise_server_exceptions=False)
    yield c
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_storage, None)
    app.dependency_overrides.pop(get_ocr_engine, None)


# ══════════════════════════════════════════════════════════════════════════════
# Seed helpers
# ══════════════════════════════════════════════════════════════════════════════

def _uid():
    return uuid.uuid4().hex[:10]


def _role_id(db, name):
    r = db.query(Role).filter(Role.name == name).first()
    assert r is not None, f"role '{name}' not found"
    return r.id


def _make_user(db, role, email=None):
    u = User(
        email=email or f"{role}_{_uid()}@p16.example.com",
        password_hash=hash_password("Test@1234"),
        role_id=_role_id(db, role),
        full_name=f"P16 {role.title()} {_uid()}",
        phone="0000000000",
    )
    db.add(u)
    db.flush()
    return u


def _token(user):
    tok = create_access_token(
        user_id=user.id,
        secret=settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
        expire_minutes=settings.access_token_expire_minutes,
    )
    return {"Authorization": f"Bearer {tok}"}


def _svc_token():
    return {"X-Service-Token": settings.internal_service_token}


def _make_dept(db, service_type="civic_services"):
    d = Department(name=f"P16Dept {_uid()}", service_type=service_type)
    db.add(d)
    db.flush()
    return d


def _make_officer(db, dept_id, role="officer"):
    u = _make_user(db, role)
    o = Officer(user_id=u.id, department_id=dept_id, designation="Officer")
    db.add(o)
    db.flush()
    return u, o


def _make_app(db, citizen_id, dept_id, status=ApplicationStatus.DOCUMENT_PENDING):
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


def _add_history(db, app_id, from_status, to_status, changed_by):
    h = ApplicationStatusHistory(
        application_id=app_id,
        from_status=from_status,
        to_status=to_status,
        changed_by=changed_by,
    )
    db.add(h)
    db.flush()
    return h


def _count_audit(db, action, entity_id=None):
    q = db.query(AuditLog).filter(AuditLog.action == action)
    if entity_id is not None:
        q = q.filter(AuditLog.entity_id == entity_id)
    return q.count()


def _last_audit(db, action, entity_id=None):
    q = db.query(AuditLog).filter(AuditLog.action == action)
    if entity_id is not None:
        q = q.filter(AuditLog.entity_id == entity_id)
    return q.order_by(AuditLog.id.desc()).first()


# ══════════════════════════════════════════════════════════════════════════════
# audit_service unit tests
# ══════════════════════════════════════════════════════════════════════════════

class TestSanitizer:
    def test_drops_forbidden_keys(self):
        dirty = {
            "password": "s3cr3t",
            "token": "abc",
            "secret": "xyz",
            "email": "a@b.com",
            "full_name": "Alice",
            "phone": "12345",
            "message": "hello",
            "remarks": "reason",
            "extracted_text": "raw ocr",
            "authorization": "Bearer tok",
            "application_id": 42,
        }
        result = sanitize_details(dirty)
        assert result is not None
        assert "password" not in result
        assert "token" not in result
        assert "secret" not in result
        assert "email" not in result
        assert "full_name" not in result
        assert "phone" not in result
        assert "message" not in result
        assert "remarks" not in result
        assert "extracted_text" not in result
        assert "authorization" not in result
        assert result["application_id"] == 42

    def test_drops_partial_forbidden_key(self):
        # Key containing 'email' substring
        result = sanitize_details({"user_email": "x@y.com", "count": 1})
        assert result is not None
        assert "user_email" not in result
        assert result["count"] == 1

    def test_caps_at_2kb(self):
        # Build a dict that is definitely > 2 KB serialized
        big = {"data": "x" * 3000}
        result = sanitize_details(big)
        assert result == {"truncated": True}

    def test_none_returns_none(self):
        assert sanitize_details(None) is None

    def test_empty_dict_after_filtering_returns_none(self):
        result = sanitize_details({"password": "secret"})
        assert result is None

    def test_allows_normal_keys(self):
        result = sanitize_details({"from_status": "SUBMITTED", "to_status": "VALIDATED"})
        assert result == {"from_status": "SUBMITTED", "to_status": "VALIDATED"}

    def test_drops_non_serializable_values(self):
        import datetime as _dt
        result = sanitize_details({"ts": _dt.datetime.now(), "count": 5})
        assert result is not None
        assert "ts" not in result
        assert result["count"] == 5


class TestRecordNoCommit:
    """record() adds a row to the session but does NOT commit."""

    def test_record_adds_row_without_commit(self, db_session):
        system_user_id = get_system_user_id(db_session)
        before = db_session.query(AuditLog).count()
        row = record(
            db_session,
            user_id=system_user_id,
            action=AuditAction.AUTH_REGISTER,
            entity_type=AuditEntity.USER,
            entity_id=9999999,
        )
        db_session.flush()
        after = db_session.query(AuditLog).count()
        assert after == before + 1

    def test_record_row_disappears_on_rollback(self, db_session):
        system_user_id = get_system_user_id(db_session)
        before = db_session.query(AuditLog).count()
        record(
            db_session,
            user_id=system_user_id,
            action=AuditAction.AUTH_REGISTER,
            entity_type=AuditEntity.USER,
            entity_id=8888888,
        )
        db_session.flush()
        db_session.rollback()
        after = db_session.query(AuditLog).count()
        assert after == before  # rolled back


class TestNoUpdateDelete:
    """The module must NOT expose update or delete functions."""

    def test_no_update_function(self):
        import app.services.audit_service as svc
        public_fns = [name for name in dir(svc) if not name.startswith("_")]
        for fn_name in public_fns:
            assert "update" not in fn_name.lower(), f"Found update fn: {fn_name}"
            assert "delete" not in fn_name.lower(), f"Found delete fn: {fn_name}"


# ══════════════════════════════════════════════════════════════════════════════
# Hook: application.create
# ══════════════════════════════════════════════════════════════════════════════

class TestHookApplicationCreate:
    def test_create_hook_written(self, client, db_session):
        citizen = _make_user(db_session, "citizen")
        dept = _make_dept(db_session)
        db_session.flush()

        r = client.post(
            "/applications",
            json={
                "department_id": dept.id,
                "service_type": "civic_services",
                "urgent": False,
            },
            headers=_token(citizen),
        )
        assert r.status_code == 201
        app_id = r.json()["id"]

        row = _last_audit(db_session, AuditAction.APPLICATION_CREATE, entity_id=app_id)
        assert row is not None
        assert row.user_id == citizen.id
        assert row.entity_type == AuditEntity.APPLICATION
        assert "priority_label" in row.details
        assert "is_duplicate" in row.details
        assert "urgent" in row.details


# ══════════════════════════════════════════════════════════════════════════════
# Hook: application.status_change
# ══════════════════════════════════════════════════════════════════════════════

class TestHookStatusChange:
    def test_status_change_hook_written_on_create(self, client, db_session):
        """Creating an app writes TWO status_change rows: SUBMITTED and DOCUMENT_PENDING."""
        citizen = _make_user(db_session, "citizen")
        dept = _make_dept(db_session)
        db_session.flush()

        r = client.post(
            "/applications",
            json={
                "department_id": dept.id,
                "service_type": "civic_services",
                "urgent": False,
            },
            headers=_token(citizen),
        )
        assert r.status_code == 201
        app_id = r.json()["id"]

        rows = (
            db_session.query(AuditLog)
            .filter(
                AuditLog.action == AuditAction.APPLICATION_STATUS_CHANGE,
                AuditLog.entity_id == app_id,
            )
            .order_by(AuditLog.id.asc())
            .all()
        )
        assert len(rows) == 2
        assert rows[0].details["to_status"] == "SUBMITTED"
        assert rows[1].details["to_status"] == "DOCUMENT_PENDING"

    def test_invariant_status_change_equals_history(self, client, db_session):
        """After create, the number of status_change audit rows == number of history rows."""
        citizen = _make_user(db_session, "citizen")
        dept = _make_dept(db_session)
        db_session.flush()

        r = client.post(
            "/applications",
            json={"department_id": dept.id, "service_type": "civic_services", "urgent": False},
            headers=_token(citizen),
        )
        assert r.status_code == 201
        app_id = r.json()["id"]

        audit_count = (
            db_session.query(AuditLog)
            .filter(
                AuditLog.action == AuditAction.APPLICATION_STATUS_CHANGE,
                AuditLog.entity_id == app_id,
            )
            .count()
        )
        history_count = (
            db_session.query(ApplicationStatusHistory)
            .filter(ApplicationStatusHistory.application_id == app_id)
            .count()
        )
        assert audit_count == history_count

    def test_invariant_after_assignment(self, db_session):
        """After create + assign, the invariant holds (includes ASSIGNED transition)."""
        citizen = _make_user(db_session, "citizen")
        dept = _make_dept(db_session)
        officer_user, officer = _make_officer(db_session, dept.id)
        db_session.flush()

        a = _make_app(db_session, citizen.id, dept.id, status=ApplicationStatus.VALIDATED)
        db_session.flush()

        # Add manual status_change rows to simulate previous transitions
        system_id = get_system_user_id(db_session)
        _add_history(db_session, a.id, None, "SUBMITTED", citizen.id)
        db_session.flush()

        from app.services.audit_service import record as _record
        _record(
            db_session,
            user_id=citizen.id,
            action=AuditAction.APPLICATION_STATUS_CHANGE,
            entity_type=AuditEntity.APPLICATION,
            entity_id=a.id,
            details={"from_status": None, "to_status": "SUBMITTED"},
        )
        db_session.flush()

        from app.services.assignment_service import assign_application
        result = assign_application(db_session, a)
        assert result is not None

        audit_count = (
            db_session.query(AuditLog)
            .filter(
                AuditLog.action == AuditAction.APPLICATION_STATUS_CHANGE,
                AuditLog.entity_id == a.id,
            )
            .count()
        )
        history_count = (
            db_session.query(ApplicationStatusHistory)
            .filter(ApplicationStatusHistory.application_id == a.id)
            .count()
        )
        assert audit_count == history_count


# ══════════════════════════════════════════════════════════════════════════════
# Hook: assignment.create
# ══════════════════════════════════════════════════════════════════════════════

class TestHookAssignmentCreate:
    def test_assignment_create_hook(self, db_session):
        """Auto-assign writes assignment.create audit row."""
        citizen = _make_user(db_session, "citizen")
        dept = _make_dept(db_session)
        officer_user, officer = _make_officer(db_session, dept.id)
        db_session.flush()

        a = _make_app(db_session, citizen.id, dept.id, status=ApplicationStatus.VALIDATED)
        db_session.flush()

        from app.services.assignment_service import assign_application
        result = assign_application(db_session, a)
        assert result is not None

        row = _last_audit(db_session, AuditAction.ASSIGNMENT_CREATE, entity_id=a.id)
        assert row is not None
        assert row.details["officer_id"] == officer.id

        # Also check the ASSIGNED status_change was written
        sc_row = (
            db_session.query(AuditLog)
            .filter(
                AuditLog.action == AuditAction.APPLICATION_STATUS_CHANGE,
                AuditLog.entity_id == a.id,
                AuditLog.details["to_status"].astext == "ASSIGNED",
            )
            .first()
        )
        assert sc_row is not None


# ══════════════════════════════════════════════════════════════════════════════
# Hook: application.request_documents
# ══════════════════════════════════════════════════════════════════════════════

class TestHookRequestDocuments:
    def test_request_documents_hook(self, client, db_session):
        citizen = _make_user(db_session, "citizen")
        dept = _make_dept(db_session)
        officer_user, officer = _make_officer(db_session, dept.id)
        db_session.flush()

        a = _make_app(db_session, citizen.id, dept.id, status=ApplicationStatus.UNDER_REVIEW)
        asn = Assignment(application_id=a.id, officer_id=officer.id)
        db_session.add(asn)
        db_session.flush()

        r = client.post(
            f"/applications/{a.id}/request-documents",
            json={"doc_types": ["PASSPORT", "AADHAAR"], "reason": "verification"},
            headers=_token(officer_user),
        )
        assert r.status_code == 200

        row = _last_audit(db_session, AuditAction.APPLICATION_REQUEST_DOCUMENTS, entity_id=a.id)
        assert row is not None
        assert row.user_id == officer_user.id
        assert "PASSPORT" in row.details["doc_types"]
        assert "AADHAAR" in row.details["doc_types"]


# ══════════════════════════════════════════════════════════════════════════════
# Hook: document.upload
# ══════════════════════════════════════════════════════════════════════════════

class TestHookDocumentUpload:
    def test_upload_hook_written(self, client, db_session):
        citizen = _make_user(db_session, "citizen")
        dept = _make_dept(db_session)
        db_session.flush()

        a = _make_app(db_session, citizen.id, dept.id, status=ApplicationStatus.DOCUMENT_PENDING)
        db_session.flush()

        fake_pdf = b"%PDF-1.4 fake"
        from io import BytesIO

        r = client.post(
            f"/applications/{a.id}/documents",
            files={"file": ("test.pdf", BytesIO(fake_pdf), "application/pdf")},
            data={"doc_type": "PASSPORT"},
            headers=_token(citizen),
        )
        assert r.status_code == 201
        doc_id = r.json()["document_id"]

        row = _last_audit(db_session, AuditAction.DOCUMENT_UPLOAD, entity_id=doc_id)
        assert row is not None
        assert row.user_id == citizen.id
        assert row.details["document_id"] == doc_id
        assert row.details["doc_type"] == "PASSPORT"


# ══════════════════════════════════════════════════════════════════════════════
# Hook: document.view
# ══════════════════════════════════════════════════════════════════════════════

class TestHookDocumentView:
    def _make_doc(self, db, app_id):
        d = Document(
            application_id=app_id,
            doc_type="PASSPORT",
            storage_ref="apps/1/abc.pdf",
            original_filename="test.pdf",
            validation_status="PENDING",
        )
        db.add(d)
        db.flush()
        return d

    def test_view_hook_written(self, client, db_session, fake_storage):
        citizen = _make_user(db_session, "citizen")
        dept = _make_dept(db_session)
        db_session.flush()
        a = _make_app(db_session, citizen.id, dept.id)
        doc = self._make_doc(db_session, a.id)
        db_session.flush()

        # Put a fake object so the stream works
        fake_storage.put("apps/1/abc.pdf", b"fake data", "application/pdf")

        r = client.get(f"/documents/{doc.id}", headers=_token(citizen))
        assert r.status_code == 200

        row = _last_audit(db_session, AuditAction.DOCUMENT_VIEW, entity_id=doc.id)
        assert row is not None
        assert row.user_id == citizen.id
        assert row.details["document_id"] == doc.id

    def test_denied_view_writes_nothing(self, client, db_session, fake_storage):
        citizen = _make_user(db_session, "citizen")
        other = _make_user(db_session, "citizen")
        dept = _make_dept(db_session)
        db_session.flush()
        a = _make_app(db_session, citizen.id, dept.id)
        doc = self._make_doc(db_session, a.id)
        db_session.flush()

        before = _count_audit(db_session, AuditAction.DOCUMENT_VIEW, entity_id=doc.id)
        r = client.get(f"/documents/{doc.id}", headers=_token(other))
        assert r.status_code == 403
        after = _count_audit(db_session, AuditAction.DOCUMENT_VIEW, entity_id=doc.id)
        assert after == before  # no row written for denied request

    def test_view_succeeds_even_if_audit_raises(self, client, db_session, fake_storage):
        """If the audit write raises inside record_standalone, the download still returns 200."""
        citizen = _make_user(db_session, "citizen")
        dept = _make_dept(db_session)
        db_session.flush()
        a = _make_app(db_session, citizen.id, dept.id)
        doc = self._make_doc(db_session, a.id)
        db_session.flush()

        fake_storage.put("apps/1/abc.pdf", b"fake data", "application/pdf")

        # Patch audit_standalone to raise — the router wraps it in try/except
        # so the download must still succeed.
        with patch(
            "app.routers.documents.audit_standalone",
            side_effect=RuntimeError("simulated audit failure"),
        ):
            r = client.get(f"/documents/{doc.id}", headers=_token(citizen))

        assert r.status_code == 200


# ══════════════════════════════════════════════════════════════════════════════
# Hook: document.analysis_view
# ══════════════════════════════════════════════════════════════════════════════

class TestHookDocumentAnalysisView:
    def _make_doc_with_analysis(self, db, app_id):
        from app.models.ai_analysis import AIAnalysis

        d = Document(
            application_id=app_id,
            doc_type="PASSPORT",
            storage_ref="apps/1/abc.pdf",
            original_filename="test.pdf",
            validation_status="VALID",
        )
        db.add(d)
        db.flush()

        ai = AIAnalysis(
            document_id=d.id,
            extracted_text="raw text",
            extracted_fields={"name": "Test"},
            validation_result={"valid": True},
            processed_at=datetime.now(timezone.utc),
        )
        db.add(ai)
        db.flush()
        return d

    def test_analysis_view_hook_written(self, client, db_session):
        citizen = _make_user(db_session, "citizen")
        dept = _make_dept(db_session)
        officer_user, officer = _make_officer(db_session, dept.id)
        db_session.flush()

        a = _make_app(db_session, citizen.id, dept.id, status=ApplicationStatus.UNDER_REVIEW)
        asn = Assignment(application_id=a.id, officer_id=officer.id)
        db_session.add(asn)
        db_session.flush()

        doc = self._make_doc_with_analysis(db_session, a.id)

        r = client.get(f"/documents/{doc.id}/analysis", headers=_token(officer_user))
        assert r.status_code == 200

        row = _last_audit(db_session, AuditAction.DOCUMENT_ANALYSIS_VIEW, entity_id=doc.id)
        assert row is not None
        assert row.user_id == officer_user.id
        assert row.details["document_id"] == doc.id

    def test_citizen_denied_analysis_writes_nothing(self, client, db_session):
        citizen = _make_user(db_session, "citizen")
        dept = _make_dept(db_session)
        db_session.flush()
        a = _make_app(db_session, citizen.id, dept.id)
        doc = Document(
            application_id=a.id,
            doc_type="PASSPORT",
            storage_ref="apps/1/abc.pdf",
            original_filename="test.pdf",
            validation_status="PENDING",
        )
        db_session.add(doc)
        db_session.flush()

        before = _count_audit(db_session, AuditAction.DOCUMENT_ANALYSIS_VIEW, entity_id=doc.id)
        r = client.get(f"/documents/{doc.id}/analysis", headers=_token(citizen))
        assert r.status_code == 403
        after = _count_audit(db_session, AuditAction.DOCUMENT_ANALYSIS_VIEW, entity_id=doc.id)
        assert after == before


# ══════════════════════════════════════════════════════════════════════════════
# Hook: escalation.create
# ══════════════════════════════════════════════════════════════════════════════

class TestHookEscalationCreate:
    def test_escalation_create_hook(self, client, db_session):
        citizen = _make_user(db_session, "citizen")
        dept = _make_dept(db_session)
        dept_head_user, dept_head_officer = _make_officer(db_session, dept.id, role="dept_head")
        db_session.flush()

        a = _make_app(db_session, citizen.id, dept.id, status=ApplicationStatus.DOCUMENT_PENDING)
        db_session.flush()

        r = client.post(
            "/internal/escalations",
            json={"application_id": a.id, "reason": "test_reason"},
            headers=_svc_token(),
        )
        assert r.status_code == 200
        esc_id = r.json()["escalation_id"]

        row = _last_audit(db_session, AuditAction.ESCALATION_CREATE, entity_id=esc_id)
        assert row is not None
        assert row.details["escalation_id"] == esc_id
        assert row.details["escalated_to_user_id"] == dept_head_user.id


# ══════════════════════════════════════════════════════════════════════════════
# Hook: escalation.resolve
# ══════════════════════════════════════════════════════════════════════════════

class TestHookEscalationResolve:
    def test_escalation_resolve_on_final_transition(self, client, db_session):
        citizen = _make_user(db_session, "citizen")
        dept = _make_dept(db_session)
        officer_user, officer = _make_officer(db_session, dept.id)
        db_session.flush()

        a = _make_app(db_session, citizen.id, dept.id, status=ApplicationStatus.UNDER_REVIEW)
        asn = Assignment(application_id=a.id, officer_id=officer.id)
        db_session.add(asn)

        escalation = Escalation(
            application_id=a.id,
            escalated_to=officer_user.id,
            reason="test",
            escalated_at=datetime.now(timezone.utc),
            resolved_at=None,
        )
        db_session.add(escalation)
        db_session.flush()

        esc_id = escalation.id

        # Approve the application (officer) — this should resolve the escalation
        r = client.post(
            f"/applications/{a.id}/decision",
            json={"decision": "approve", "remarks": "looks good"},
            headers=_token(officer_user),
        )
        assert r.status_code == 200

        row = _last_audit(db_session, AuditAction.ESCALATION_RESOLVE, entity_id=esc_id)
        assert row is not None
        assert row.details["escalation_id"] == esc_id
        assert row.user_id == officer_user.id


# ══════════════════════════════════════════════════════════════════════════════
# Hook: notification.send
# ══════════════════════════════════════════════════════════════════════════════

class TestHookNotificationSend:
    def test_notification_send_hook_on_success(self, client, db_session):
        citizen = _make_user(db_session, "citizen")
        dept = _make_dept(db_session)
        db_session.flush()
        a = _make_app(db_session, citizen.id, dept.id)
        db_session.flush()

        from app.services.notification_service import LogProvider
        with patch("app.routers.internal.get_provider", return_value=LogProvider()):
            r = client.post(
                "/internal/notifications/send",
                json={
                    "application_id": a.id,
                    "recipient_id": citizen.id,
                    "channel": "email",
                    "message": "Your application is under review.",
                },
                headers=_svc_token(),
            )
        assert r.status_code == 200
        notif_id = r.json()["notification_id"]

        row = _last_audit(db_session, AuditAction.NOTIFICATION_SEND, entity_id=notif_id)
        assert row is not None
        assert row.details["notification_id"] == notif_id
        assert row.details["status"] == "sent"

    def test_notification_send_hook_on_failure(self, client, db_session):
        citizen = _make_user(db_session, "citizen")
        dept = _make_dept(db_session)
        db_session.flush()
        a = _make_app(db_session, citizen.id, dept.id)
        db_session.flush()

        # Use a provider that always fails
        class _FailProvider:
            def send(self, *args, **kwargs):
                return ("failed", "test_reason")

        with patch("app.routers.internal.get_provider", return_value=_FailProvider()):
            r = client.post(
                "/internal/notifications/send",
                json={
                    "application_id": a.id,
                    "recipient_id": citizen.id,
                    "channel": "email",
                    "message": "Test message.",
                },
                headers=_svc_token(),
            )
        assert r.status_code == 500  # delivery failed

        # Audit row should still be written with status=failed
        row = (
            db_session.query(AuditLog)
            .filter(
                AuditLog.action == AuditAction.NOTIFICATION_SEND,
                AuditLog.details["status"].astext == "failed",
            )
            .order_by(AuditLog.id.desc())
            .first()
        )
        assert row is not None
        assert row.details["status"] == "failed"


# ══════════════════════════════════════════════════════════════════════════════
# Hook: priority.reevaluate
# ══════════════════════════════════════════════════════════════════════════════

class TestHookPriorityReevaluate:
    def test_reevaluate_hook_written(self, client, db_session):
        before = _count_audit(db_session, AuditAction.PRIORITY_REEVALUATE)

        r = client.post("/internal/priority/reevaluate", headers=_svc_token())
        assert r.status_code == 200

        after = _count_audit(db_session, AuditAction.PRIORITY_REEVALUATE)
        assert after == before + 1

        row = _last_audit(db_session, AuditAction.PRIORITY_REEVALUATE)
        assert row is not None
        assert "updated" in row.details


# ══════════════════════════════════════════════════════════════════════════════
# Hook: auth.register
# ══════════════════════════════════════════════════════════════════════════════

class TestHookAuthRegister:
    def test_register_hook_written(self, client, db_session):
        uid = _uid()
        r = client.post(
            "/auth/register",
            json={
                "email": f"regtest_{uid}@p16.example.com",
                "password": "Test@1234",
                "full_name": "Reg Test",
                "phone": "1234567890",
            },
        )
        assert r.status_code == 201
        user_id = r.json()["user_id"]

        row = _last_audit(db_session, AuditAction.AUTH_REGISTER, entity_id=user_id)
        assert row is not None
        assert row.user_id == user_id
        assert row.entity_type == AuditEntity.USER


# ══════════════════════════════════════════════════════════════════════════════
# Hook: auth.login_success
# ══════════════════════════════════════════════════════════════════════════════

class TestHookAuthLoginSuccess:
    def test_login_success_hook_written(self, client, db_session):
        uid = _uid()
        email = f"loginsuccess_{uid}@p16.example.com"
        user = _make_user(db_session, "citizen", email=email)
        db_session.flush()

        before = _count_audit(db_session, AuditAction.AUTH_LOGIN_SUCCESS, entity_id=user.id)
        r = client.post("/auth/login", json={"email": email, "password": "Test@1234"})
        assert r.status_code == 200
        after = _count_audit(db_session, AuditAction.AUTH_LOGIN_SUCCESS, entity_id=user.id)
        assert after == before + 1

        row = _last_audit(db_session, AuditAction.AUTH_LOGIN_SUCCESS, entity_id=user.id)
        assert row is not None
        assert row.user_id == user.id


# ══════════════════════════════════════════════════════════════════════════════
# Hook: auth.login_failure (bad_password)
# ══════════════════════════════════════════════════════════════════════════════

class TestHookAuthLoginFailureBadPassword:
    def test_bad_password_hook_written(self, client, db_session):
        uid = _uid()
        email = f"badpw_{uid}@p16.example.com"
        user = _make_user(db_session, "citizen", email=email)
        db_session.flush()

        before = _count_audit(db_session, AuditAction.AUTH_LOGIN_FAILURE, entity_id=user.id)
        r = client.post("/auth/login", json={"email": email, "password": "WRONG_PW"})
        assert r.status_code == 401
        after = _count_audit(db_session, AuditAction.AUTH_LOGIN_FAILURE, entity_id=user.id)
        assert after == before + 1

        row = _last_audit(db_session, AuditAction.AUTH_LOGIN_FAILURE, entity_id=user.id)
        assert row is not None
        assert row.details["reason"] == "bad_password"


# ══════════════════════════════════════════════════════════════════════════════
# Hook: auth.login_failure (system_account)
# ══════════════════════════════════════════════════════════════════════════════

class TestHookAuthLoginFailureSystemAccount:
    def test_system_account_hook_written(self, client, db_session):
        from app.models.user import User as _User
        system_user = (
            db_session.query(_User)
            .filter(_User.email == "system@civicflow.internal")
            .first()
        )
        assert system_user is not None

        before = _count_audit(db_session, AuditAction.AUTH_LOGIN_FAILURE, entity_id=system_user.id)
        r = client.post(
            "/auth/login",
            json={"email": "system@civicflow.internal", "password": "any_password"},
        )
        assert r.status_code == 401
        after = _count_audit(db_session, AuditAction.AUTH_LOGIN_FAILURE, entity_id=system_user.id)
        assert after == before + 1

        row = _last_audit(db_session, AuditAction.AUTH_LOGIN_FAILURE, entity_id=system_user.id)
        assert row is not None
        assert row.details["reason"] == "system_account"


# ══════════════════════════════════════════════════════════════════════════════
# Unknown-email login writes NO audit row
# ══════════════════════════════════════════════════════════════════════════════

class TestUnknownEmailNoAudit:
    def test_unknown_email_writes_nothing(self, client, db_session):
        before = db_session.query(AuditLog).count()
        r = client.post(
            "/auth/login",
            json={"email": f"nobody_{_uid()}@p16.example.com", "password": "whatever"},
        )
        assert r.status_code == 401
        after = db_session.query(AuditLog).count()
        assert after == before  # no rows added


# ══════════════════════════════════════════════════════════════════════════════
# Atomicity: if a commit raises, no audit row remains
# ══════════════════════════════════════════════════════════════════════════════

class TestAtomicity:
    def test_failed_transition_leaves_no_audit_row(self, db_session):
        """record() + rollback leaves no audit row."""
        system_id = get_system_user_id(db_session)

        before = db_session.query(AuditLog).count()

        sp = db_session.begin_nested()
        try:
            record(
                db_session,
                user_id=system_id,
                action=AuditAction.APPLICATION_STATUS_CHANGE,
                entity_type=AuditEntity.APPLICATION,
                entity_id=9999998,
                details={"from_status": "X", "to_status": "Y"},
            )
            db_session.flush()
            raise RuntimeError("simulated commit failure")
        except RuntimeError:
            sp.rollback()

        after = db_session.query(AuditLog).count()
        assert after == before


# ══════════════════════════════════════════════════════════════════════════════
# Login audit resilience: audit raise must not block login
# ══════════════════════════════════════════════════════════════════════════════

class TestLoginAuditResilience:
    def test_audit_failure_does_not_block_login(self, client, db_session):
        uid = _uid()
        email = f"resilience_{uid}@p16.example.com"
        user = _make_user(db_session, "citizen", email=email)
        db_session.flush()

        # Patch audit_standalone to raise — auth.py wraps it in try/except
        # so login must still succeed.
        with patch(
            "app.routers.auth.audit_standalone",
            side_effect=RuntimeError("simulated audit failure"),
        ):
            r = client.post("/auth/login", json={"email": email, "password": "Test@1234"})

        # Login must succeed even if audit write raises
        assert r.status_code == 200


# ══════════════════════════════════════════════════════════════════════════════
# No PII in audit details
# ══════════════════════════════════════════════════════════════════════════════

class TestNoPII:
    def test_no_pii_in_audit_details(self, client, db_session):
        """After creating a citizen and application, no details value contains PII."""
        uid = _uid()
        pii_email = f"piicitizen_{uid}@p16.example.com"
        pii_name = f"PII Citizen {uid}"
        pii_phone = f"9{uid[:9]}"

        # Register a citizen via the API
        r = client.post(
            "/auth/register",
            json={
                "email": pii_email,
                "password": "Test@1234",
                "full_name": pii_name,
                "phone": pii_phone,
            },
        )
        assert r.status_code == 201
        user_id = r.json()["user_id"]

        dept = _make_dept(db_session)
        db_session.flush()

        # Create an application
        user_tok = create_access_token(
            user_id=user_id,
            secret=settings.jwt_secret_key,
            algorithm=settings.jwt_algorithm,
            expire_minutes=settings.access_token_expire_minutes,
        )
        client.post(
            "/applications",
            json={"department_id": dept.id, "service_type": "civic_services", "urgent": False},
            headers={"Authorization": f"Bearer {user_tok}"},
        )

        # Scan all audit detail values for PII
        rows = db_session.query(AuditLog).all()
        for row in rows:
            if row.details is None:
                continue
            details_str = json.dumps(row.details)
            assert pii_email not in details_str, f"Email found in audit details: {details_str}"
            assert pii_name not in details_str, f"Full name found in audit details: {details_str}"
            assert pii_phone not in details_str, f"Phone found in audit details: {details_str}"
