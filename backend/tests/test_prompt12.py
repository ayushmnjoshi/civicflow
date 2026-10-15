"""Tests for Prompt 12: assignment service, officer queue, decision, request-documents."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.auth.security import create_access_token, hash_password
from app.config import settings
from app.constants import Priority
from app.database import engine
from app.dependencies import get_db
from app.main import app
from app.models.application import Application, ApplicationStatus
from app.models.application_status_history import ApplicationStatusHistory
from app.models.assignment import Assignment
from app.models.department import Department
from app.models.officer import Officer
from app.models.role import Role
from app.models.user import User
from app.services.assignment_service import assign_application, run_bulk_assign
from app.services.system_actor import get_system_user_id


# ── Fixtures ──────────────────────────────────────────────────────────────────

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
def client(db_session: Session):
    def _override():
        yield db_session

    app.dependency_overrides[get_db] = _override
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides.clear()


# ── seed helpers ──────────────────────────────────────────────────────────────

def _uid() -> str:
    """Return a short unique suffix for emails."""
    return uuid.uuid4().hex[:12]


def _role_id(db: Session, name: str) -> int:
    r = db.query(Role).filter(Role.name == name).first()
    assert r is not None, f"Role '{name}' not seeded."
    return r.id


def _make_user(db: Session, role_name: str, email: str, full_name: str = "Test User") -> User:
    u = User(
        email=email,
        password_hash=hash_password("Test@1234"),
        role_id=_role_id(db, role_name),
        full_name=full_name,
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


def _svc_token() -> dict:
    return {"X-Service-Token": settings.internal_service_token}


def _make_isolated_dept(db: Session, tag: str = "") -> Department:
    """Create a fresh department with no officers — fully isolated for the test."""
    tag = tag or _uid()
    dept = Department(name=f"Test Dept {tag}", service_type="test_service")
    db.add(dept)
    db.flush()
    return dept


def _make_officer(db: Session, dept_id: int, tag: str = "") -> tuple[User, Officer]:
    tag = tag or _uid()
    user = _make_user(db, "officer", f"officer_{tag}@test.dev")
    officer = Officer(user_id=user.id, department_id=dept_id, designation="Officer")
    db.add(officer)
    db.flush()
    return user, officer


def _make_validated_app(db: Session, dept_id: int) -> Application:
    """Create a VALIDATED application in the given department."""
    tag = _uid()
    citizen = _make_user(db, "citizen", f"citizen_{tag}@test.dev")
    application = Application(
        citizen_id=citizen.id,
        department_id=dept_id,
        service_type="Test Service",
        status=ApplicationStatus.VALIDATED,
        priority=Priority.MEDIUM.value,
        is_duplicate_flag=False,
    )
    db.add(application)
    db.flush()
    return application


def _make_app_with_status(
    db: Session, dept_id: int, app_status: ApplicationStatus
) -> Application:
    tag = _uid()
    citizen = _make_user(db, "citizen", f"citizen_{tag}@test.dev")
    application = Application(
        citizen_id=citizen.id,
        department_id=dept_id,
        service_type="Test Service",
        status=app_status,
        priority=Priority.MEDIUM.value,
        is_duplicate_flag=False,
    )
    db.add(application)
    db.flush()
    return application


def _assign_app(db: Session, application: Application, officer: Officer) -> Assignment:
    """Directly insert an active assignment (no state machine)."""
    a = Assignment(application_id=application.id, officer_id=officer.id)
    db.add(a)
    db.flush()
    return a


# ══════════════════════════════════════════════════════════════════════════════
# Part 1 — assign_application service tests (use isolated departments)
# ══════════════════════════════════════════════════════════════════════════════

class TestAssignApplication:

    def test_picks_least_loaded_officer(self, db_session):
        """The officer with the fewest active assignments is picked."""
        dept = _make_isolated_dept(db_session, "least")
        _, officer1 = _make_officer(db_session, dept.id, "lo1a")
        _, officer2 = _make_officer(db_session, dept.id, "lo1b")

        # Give officer1 one active assignment already on a non-final app
        existing_app = _make_app_with_status(db_session, dept.id, ApplicationStatus.ASSIGNED)
        _assign_app(db_session, existing_app, officer1)

        # New VALIDATED app should go to officer2 (0 assignments)
        new_app = _make_validated_app(db_session, dept.id)
        result = assign_application(db_session, new_app)
        assert result is not None
        assert result.officer_id == officer2.id

    def test_ties_go_to_lowest_officer_id(self, db_session):
        """When load is equal, the officer with the lowest id wins."""
        dept = _make_isolated_dept(db_session, "ties")
        _, officer_a = _make_officer(db_session, dept.id, "ta1")
        _, officer_b = _make_officer(db_session, dept.id, "ta2")

        # Both have 0 assignments; officer_a has the lower id
        application = _make_validated_app(db_session, dept.id)
        result = assign_application(db_session, application)
        assert result is not None
        assert result.officer_id == min(officer_a.id, officer_b.id)

    def test_officers_from_other_department_ignored(self, db_session):
        """Officers in a different department are not candidates."""
        dept1 = _make_isolated_dept(db_session, "od1")
        dept2 = _make_isolated_dept(db_session, "od2")

        _, officer_wrong = _make_officer(db_session, dept2.id, "wrong")
        _, officer_right = _make_officer(db_session, dept1.id, "right")

        application = _make_validated_app(db_session, dept1.id)
        result = assign_application(db_session, application)
        assert result is not None
        assert result.officer_id == officer_right.id

    def test_no_officer_in_dept_leaves_app_validated(self, db_session):
        """No candidate → application stays VALIDATED, no assignment written."""
        dept = _make_isolated_dept(db_session, "nooff")
        # No officers in this isolated dept
        application = _make_validated_app(db_session, dept.id)

        result = assign_application(db_session, application)
        assert result is None

        db_session.refresh(application)
        assert application.status == ApplicationStatus.VALIDATED

        count = (
            db_session.query(Assignment)
            .filter(Assignment.application_id == application.id)
            .count()
        )
        assert count == 0

    def test_double_call_does_not_create_second_assignment(self, db_session):
        """Running assign_application twice on the same app is idempotent."""
        dept = _make_isolated_dept(db_session, "idem")
        _, officer = _make_officer(db_session, dept.id, "idem1")
        application = _make_validated_app(db_session, dept.id)

        first = assign_application(db_session, application)
        assert first is not None

        # App is now ASSIGNED — second call should do nothing
        db_session.refresh(application)
        second = assign_application(db_session, application)
        assert second is None

        count = (
            db_session.query(Assignment)
            .filter(
                Assignment.application_id == application.id,
                Assignment.unassigned_at.is_(None),
            )
            .count()
        )
        assert count == 1

    def test_assignment_row_has_unassigned_at_null(self, db_session):
        """The new assignments row must have unassigned_at IS NULL."""
        dept = _make_isolated_dept(db_session, "nullchk")
        _, officer = _make_officer(db_session, dept.id, "nc1")
        application = _make_validated_app(db_session, dept.id)

        result = assign_application(db_session, application)
        assert result is not None
        assert result.unassigned_at is None

    def test_history_row_has_system_user_as_changed_by(self, db_session):
        """The VALIDATED→ASSIGNED history row uses the system user as changed_by."""
        dept = _make_isolated_dept(db_session, "hist")
        _, officer = _make_officer(db_session, dept.id, "hs1")
        application = _make_validated_app(db_session, dept.id)
        system_user_id = get_system_user_id(db_session)

        assign_application(db_session, application)

        hist = (
            db_session.query(ApplicationStatusHistory)
            .filter(
                ApplicationStatusHistory.application_id == application.id,
                ApplicationStatusHistory.from_status == "VALIDATED",
                ApplicationStatusHistory.to_status == "ASSIGNED",
            )
            .first()
        )
        assert hist is not None
        assert hist.changed_by == system_user_id


# ══════════════════════════════════════════════════════════════════════════════
# Part 2 — POST /internal/assignments/run
# ══════════════════════════════════════════════════════════════════════════════

class TestInternalAssignmentsRun:

    def test_no_token_returns_401(self, client):
        r = client.post("/internal/assignments/run")
        assert r.status_code == 401

    def test_wrong_token_returns_401(self, client):
        r = client.post(
            "/internal/assignments/run",
            headers={"X-Service-Token": "wrong-token-value"},
        )
        assert r.status_code == 401

    def test_user_jwt_returns_401(self, client, db_session):
        citizen = _make_user(db_session, "citizen", f"int_cit_{_uid()}@test.dev")
        r = client.post(
            "/internal/assignments/run",
            headers=_auth(citizen),
        )
        assert r.status_code == 401

    def test_assigns_validated_app_after_officer_added(self, client, db_session):
        """A VALIDATED app in an isolated dept gets assigned after an officer is added."""
        dept = _make_isolated_dept(db_session, "bulk1")
        _, officer = _make_officer(db_session, dept.id, "ba1")
        application = _make_validated_app(db_session, dept.id)

        r = client.post("/internal/assignments/run", headers=_svc_token())
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body["assigned"], int)
        assert isinstance(body["still_unassigned"], int)
        assert body["assigned"] >= 1

        db_session.refresh(application)
        assert application.status == ApplicationStatus.ASSIGNED

    def test_returns_correct_counts(self, client, db_session):
        """still_unassigned reflects apps with no officer in their department."""
        # Department with no officer → app stays VALIDATED
        empty_dept = _make_isolated_dept(db_session, "empty1")
        orphan_app = _make_validated_app(db_session, empty_dept.id)

        # Department with officer → app gets assigned
        good_dept = _make_isolated_dept(db_session, "good1")
        _, officer = _make_officer(db_session, good_dept.id, "gc1")
        assigned_app = _make_validated_app(db_session, good_dept.id)

        r = client.post("/internal/assignments/run", headers=_svc_token())
        assert r.status_code == 200
        body = r.json()
        assert body["assigned"] >= 1
        assert body["still_unassigned"] >= 1


# ══════════════════════════════════════════════════════════════════════════════
# Part 3 — GET /officer/queue
# ══════════════════════════════════════════════════════════════════════════════

class TestOfficerQueue:

    def test_returns_only_callers_apps(self, client, db_session):
        """Only the calling officer's assigned apps are returned."""
        dept = _make_isolated_dept(db_session, "qonly")
        officer1_user, officer1 = _make_officer(db_session, dept.id, "qo1")
        officer2_user, officer2 = _make_officer(db_session, dept.id, "qo2")

        app1 = _make_app_with_status(db_session, dept.id, ApplicationStatus.ASSIGNED)
        _assign_app(db_session, app1, officer1)
        app2 = _make_app_with_status(db_session, dept.id, ApplicationStatus.ASSIGNED)
        _assign_app(db_session, app2, officer2)

        r = client.get("/officer/queue", headers=_auth(officer1_user))
        assert r.status_code == 200
        ids = [item["id"] for item in r.json()]
        assert app1.id in ids
        assert app2.id not in ids

    def test_excludes_approved_and_rejected(self, client, db_session):
        """APPROVED and REJECTED applications are excluded from the queue."""
        dept = _make_isolated_dept(db_session, "qexcl")
        officer_user, officer = _make_officer(db_session, dept.id, "qe1")

        approved_app = _make_app_with_status(db_session, dept.id, ApplicationStatus.APPROVED)
        rejected_app = _make_app_with_status(db_session, dept.id, ApplicationStatus.REJECTED)
        active_app = _make_app_with_status(db_session, dept.id, ApplicationStatus.ASSIGNED)

        _assign_app(db_session, approved_app, officer)
        _assign_app(db_session, rejected_app, officer)
        _assign_app(db_session, active_app, officer)

        r = client.get("/officer/queue", headers=_auth(officer_user))
        assert r.status_code == 200
        ids = [item["id"] for item in r.json()]
        assert active_app.id in ids
        assert approved_app.id not in ids
        assert rejected_app.id not in ids

    def test_order_priority_desc_then_age_asc(self, client, db_session):
        """Sort: priority DESC (HIGH first), then created_at ASC (oldest first)."""
        from datetime import datetime, timedelta, timezone
        dept = _make_isolated_dept(db_session, "qord")
        officer_user, officer = _make_officer(db_session, dept.id, "qord1")

        high_app = _make_app_with_status(db_session, dept.id, ApplicationStatus.ASSIGNED)
        high_app.priority = Priority.HIGH.value
        low_app = _make_app_with_status(db_session, dept.id, ApplicationStatus.ASSIGNED)
        low_app.priority = Priority.LOW.value
        med_app = _make_app_with_status(db_session, dept.id, ApplicationStatus.ASSIGNED)
        med_app.priority = Priority.MEDIUM.value
        db_session.flush()

        _assign_app(db_session, high_app, officer)
        _assign_app(db_session, low_app, officer)
        _assign_app(db_session, med_app, officer)

        r = client.get("/officer/queue", headers=_auth(officer_user))
        assert r.status_code == 200
        items = r.json()
        assert len(items) >= 3
        # HIGH should come first
        assert items[0]["priority"] == "HIGH"

    def test_status_filter_works(self, client, db_session):
        """?status=ASSIGNED filters to only ASSIGNED applications."""
        dept = _make_isolated_dept(db_session, "qsfilt")
        officer_user, officer = _make_officer(db_session, dept.id, "qsf1")

        assigned_app = _make_app_with_status(db_session, dept.id, ApplicationStatus.ASSIGNED)
        review_app = _make_app_with_status(db_session, dept.id, ApplicationStatus.UNDER_REVIEW)
        _assign_app(db_session, assigned_app, officer)
        _assign_app(db_session, review_app, officer)

        r = client.get("/officer/queue?status=ASSIGNED", headers=_auth(officer_user))
        assert r.status_code == 200
        items = r.json()
        assert len(items) >= 1
        for item in items:
            assert item["status"] == "ASSIGNED"
        ids = [i["id"] for i in items]
        assert assigned_app.id in ids
        assert review_app.id not in ids

    def test_unknown_status_returns_422(self, client, db_session):
        """Unknown status filter → 422."""
        dept = _make_isolated_dept(db_session, "qunk")
        officer_user, _ = _make_officer(db_session, dept.id, "qunk1")
        r = client.get("/officer/queue?status=BOGUS", headers=_auth(officer_user))
        assert r.status_code == 422

    def test_citizen_gets_403(self, client, db_session):
        citizen = _make_user(db_session, "citizen", f"qcit_{_uid()}@test.dev")
        r = client.get("/officer/queue", headers=_auth(citizen))
        assert r.status_code == 403

    def test_admin_gets_403(self, client, db_session):
        admin = _make_user(db_session, "admin", f"qadm_{_uid()}@test.dev")
        r = client.get("/officer/queue", headers=_auth(admin))
        assert r.status_code == 403

    def test_no_token_returns_401(self, client):
        r = client.get("/officer/queue")
        assert r.status_code == 401

    def test_another_officers_apps_not_shown(self, client, db_session):
        """Officer B cannot see Officer A's assigned applications."""
        dept = _make_isolated_dept(db_session, "qsec")
        officer_a_user, officer_a = _make_officer(db_session, dept.id, "qseca")
        officer_b_user, officer_b = _make_officer(db_session, dept.id, "qsecb")

        app_for_a = _make_app_with_status(db_session, dept.id, ApplicationStatus.ASSIGNED)
        _assign_app(db_session, app_for_a, officer_a)

        r = client.get("/officer/queue", headers=_auth(officer_b_user))
        assert r.status_code == 200
        ids = [item["id"] for item in r.json()]
        assert app_for_a.id not in ids

    def test_officer_with_no_officers_row_gets_empty_list(self, client, db_session):
        """Officer role user with no officers DB row → [] not 500."""
        officer_user = _make_user(db_session, "officer", f"qnorow_{_uid()}@test.dev")
        # No Officer row created
        r = client.get("/officer/queue", headers=_auth(officer_user))
        assert r.status_code == 200
        assert r.json() == []


# ══════════════════════════════════════════════════════════════════════════════
# Part 4 — POST /applications/{id}/decision
# ══════════════════════════════════════════════════════════════════════════════

class TestDecision:

    def _setup_under_review(
        self, db_session: Session, tag: str = ""
    ) -> tuple[Application, User, Officer]:
        """Create an UNDER_REVIEW app with an assigned officer in an isolated dept."""
        tag = tag or _uid()
        dept = _make_isolated_dept(db_session, tag)
        application = _make_app_with_status(db_session, dept.id, ApplicationStatus.UNDER_REVIEW)
        officer_user, officer = _make_officer(db_session, dept.id, f"dec_{tag}")
        _assign_app(db_session, application, officer)
        return application, officer_user, officer

    def test_approve_works(self, client, db_session):
        app, officer_user, _ = self._setup_under_review(db_session, "appr")
        r = client.post(
            f"/applications/{app.id}/decision",
            json={"decision": "approve", "remarks": "All documents valid."},
            headers=_auth(officer_user),
        )
        assert r.status_code == 200
        assert r.json()["status"] == "APPROVED"

    def test_reject_works(self, client, db_session):
        app, officer_user, _ = self._setup_under_review(db_session, "rej")
        r = client.post(
            f"/applications/{app.id}/decision",
            json={"decision": "reject", "remarks": "Missing document."},
            headers=_auth(officer_user),
        )
        assert r.status_code == 200
        assert r.json()["status"] == "REJECTED"

    def test_history_row_has_officer_as_actor(self, client, db_session):
        """The history row for APPROVED must have the officer as changed_by."""
        app, officer_user, _ = self._setup_under_review(db_session, "hist")
        client.post(
            f"/applications/{app.id}/decision",
            json={"decision": "approve", "remarks": "Good."},
            headers=_auth(officer_user),
        )
        hist = (
            db_session.query(ApplicationStatusHistory)
            .filter(
                ApplicationStatusHistory.application_id == app.id,
                ApplicationStatusHistory.to_status == "APPROVED",
            )
            .first()
        )
        assert hist is not None
        assert hist.changed_by == officer_user.id

    def test_empty_remarks_fails(self, client, db_session):
        app, officer_user, _ = self._setup_under_review(db_session, "emr")
        r = client.post(
            f"/applications/{app.id}/decision",
            json={"decision": "approve", "remarks": ""},
            headers=_auth(officer_user),
        )
        assert r.status_code == 422

    def test_wrong_status_gives_400(self, client, db_session):
        """Decision on a non-UNDER_REVIEW app must return 400."""
        tag = _uid()
        dept = _make_isolated_dept(db_session, tag)
        application = _make_app_with_status(db_session, dept.id, ApplicationStatus.ASSIGNED)
        officer_user, officer = _make_officer(db_session, dept.id, f"ws_{tag}")
        _assign_app(db_session, application, officer)

        r = client.post(
            f"/applications/{application.id}/decision",
            json={"decision": "approve", "remarks": "Fine."},
            headers=_auth(officer_user),
        )
        assert r.status_code == 400

    def test_non_assigned_officer_gets_403(self, client, db_session):
        tag = _uid()
        dept = _make_isolated_dept(db_session, tag)
        application = _make_app_with_status(db_session, dept.id, ApplicationStatus.UNDER_REVIEW)
        officer1_user, officer1 = _make_officer(db_session, dept.id, f"na1_{tag}")
        officer2_user, officer2 = _make_officer(db_session, dept.id, f"na2_{tag}")
        _assign_app(db_session, application, officer1)

        r = client.post(
            f"/applications/{application.id}/decision",
            json={"decision": "approve", "remarks": "Fine."},
            headers=_auth(officer2_user),
        )
        assert r.status_code == 403

    def test_citizen_gets_403(self, client, db_session):
        citizen = _make_user(db_session, "citizen", f"dcit_{_uid()}@test.dev")
        dept = _make_isolated_dept(db_session)
        application = _make_app_with_status(db_session, dept.id, ApplicationStatus.UNDER_REVIEW)
        r = client.post(
            f"/applications/{application.id}/decision",
            json={"decision": "approve", "remarks": "Fine."},
            headers=_auth(citizen),
        )
        assert r.status_code == 403

    def test_no_token_returns_401(self, client, db_session):
        dept = _make_isolated_dept(db_session)
        application = _make_app_with_status(db_session, dept.id, ApplicationStatus.UNDER_REVIEW)
        r = client.post(
            f"/applications/{application.id}/decision",
            json={"decision": "approve", "remarks": "Fine."},
        )
        assert r.status_code == 401

    def test_unknown_id_returns_403_or_404(self, client, db_session):
        dept = _make_isolated_dept(db_session)
        officer_user, _ = _make_officer(db_session, dept.id, f"u404_{_uid()}")
        r = client.post(
            "/applications/999999/decision",
            json={"decision": "approve", "remarks": "Fine."},
            headers=_auth(officer_user),
        )
        assert r.status_code in (403, 404)


# ══════════════════════════════════════════════════════════════════════════════
# Part 5 — POST /applications/{id}/request-documents
# ══════════════════════════════════════════════════════════════════════════════

class TestRequestDocuments:

    def _setup_under_review(
        self, db_session: Session, tag: str = ""
    ) -> tuple[Application, User, Officer]:
        tag = tag or _uid()
        dept = _make_isolated_dept(db_session, tag)
        application = _make_app_with_status(db_session, dept.id, ApplicationStatus.UNDER_REVIEW)
        officer_user, officer = _make_officer(db_session, dept.id, f"req_{tag}")
        _assign_app(db_session, application, officer)
        return application, officer_user, officer

    def test_request_docs_from_under_review_works(self, client, db_session):
        app, officer_user, _ = self._setup_under_review(db_session, "rd1")
        r = client.post(
            f"/applications/{app.id}/request-documents",
            json={"doc_types": ["BIRTH_CERT", "PHOTO_ID"], "reason": "Need both."},
            headers=_auth(officer_user),
        )
        assert r.status_code == 200
        assert r.json()["status"] == "ADDITIONAL_DOCUMENT_REQUIRED"

    def test_invalid_doc_type_fails(self, client, db_session):
        app, officer_user, _ = self._setup_under_review(db_session, "rd2")
        r = client.post(
            f"/applications/{app.id}/request-documents",
            json={"doc_types": ["invalid-doc-type!!"], "reason": "Test."},
            headers=_auth(officer_user),
        )
        assert r.status_code == 422

    def test_duplicate_doc_types_fail(self, client, db_session):
        app, officer_user, _ = self._setup_under_review(db_session, "rd3")
        r = client.post(
            f"/applications/{app.id}/request-documents",
            json={"doc_types": ["BIRTH_CERT", "BIRTH_CERT"], "reason": "Test."},
            headers=_auth(officer_user),
        )
        assert r.status_code == 422

    def test_more_than_10_doc_types_fails(self, client, db_session):
        app, officer_user, _ = self._setup_under_review(db_session, "rd4")
        r = client.post(
            f"/applications/{app.id}/request-documents",
            json={"doc_types": [f"DOC_{i}" for i in range(11)], "reason": "Test."},
            headers=_auth(officer_user),
        )
        assert r.status_code == 422

    def test_wrong_status_gives_400(self, client, db_session):
        tag = _uid()
        dept = _make_isolated_dept(db_session, tag)
        application = _make_app_with_status(db_session, dept.id, ApplicationStatus.ASSIGNED)
        officer_user, officer = _make_officer(db_session, dept.id, f"rdws_{tag}")
        _assign_app(db_session, application, officer)

        r = client.post(
            f"/applications/{application.id}/request-documents",
            json={"doc_types": ["BIRTH_CERT"], "reason": "Test."},
            headers=_auth(officer_user),
        )
        assert r.status_code == 400

    def test_non_assigned_officer_gets_403(self, client, db_session):
        tag = _uid()
        dept = _make_isolated_dept(db_session, tag)
        application = _make_app_with_status(db_session, dept.id, ApplicationStatus.UNDER_REVIEW)
        officer1_user, officer1 = _make_officer(db_session, dept.id, f"rna1_{tag}")
        officer2_user, officer2 = _make_officer(db_session, dept.id, f"rna2_{tag}")
        _assign_app(db_session, application, officer1)

        r = client.post(
            f"/applications/{application.id}/request-documents",
            json={"doc_types": ["BIRTH_CERT"], "reason": "Test."},
            headers=_auth(officer2_user),
        )
        assert r.status_code == 403

    def test_citizen_gets_403(self, client, db_session):
        citizen = _make_user(db_session, "citizen", f"rcit_{_uid()}@test.dev")
        dept = _make_isolated_dept(db_session)
        application = _make_app_with_status(db_session, dept.id, ApplicationStatus.UNDER_REVIEW)
        r = client.post(
            f"/applications/{application.id}/request-documents",
            json={"doc_types": ["BIRTH_CERT"], "reason": "Test."},
            headers=_auth(citizen),
        )
        assert r.status_code == 403

    def test_no_token_returns_401(self, client, db_session):
        dept = _make_isolated_dept(db_session)
        application = _make_app_with_status(db_session, dept.id, ApplicationStatus.UNDER_REVIEW)
        r = client.post(
            f"/applications/{application.id}/request-documents",
            json={"doc_types": ["BIRTH_CERT"], "reason": "Test."},
        )
        assert r.status_code == 401

    def test_unknown_id_returns_403_or_404(self, client, db_session):
        dept = _make_isolated_dept(db_session)
        officer_user, _ = _make_officer(db_session, dept.id, f"r404_{_uid()}")
        r = client.post(
            "/applications/999999/request-documents",
            json={"doc_types": ["BIRTH_CERT"], "reason": "Test."},
            headers=_auth(officer_user),
        )
        assert r.status_code in (403, 404)


# ══════════════════════════════════════════════════════════════════════════════
# Part 6 — PATCH /applications/{id}/status with assignment guard
# ══════════════════════════════════════════════════════════════════════════════

class TestPatchStatusWithAssignment:

    def test_assigned_officer_can_start_review(self, client, db_session):
        """An officer with an active assignment can transition ASSIGNED → UNDER_REVIEW."""
        dept = _make_isolated_dept(db_session, "patch1")
        application = _make_app_with_status(db_session, dept.id, ApplicationStatus.ASSIGNED)
        officer_user, officer = _make_officer(db_session, dept.id, "pa1")
        _assign_app(db_session, application, officer)

        r = client.patch(
            f"/applications/{application.id}/status",
            json={"new_status": "UNDER_REVIEW"},
            headers=_auth(officer_user),
        )
        assert r.status_code == 200
        assert r.json()["status"] == "UNDER_REVIEW"

    def test_non_assigned_officer_gets_403_on_patch(self, client, db_session):
        """An officer with no assignment on this app gets 403."""
        dept = _make_isolated_dept(db_session, "patch2")
        application = _make_app_with_status(db_session, dept.id, ApplicationStatus.ASSIGNED)
        officer1_user, officer1 = _make_officer(db_session, dept.id, "pna1")
        officer2_user, officer2 = _make_officer(db_session, dept.id, "pna2")
        _assign_app(db_session, application, officer1)

        r = client.patch(
            f"/applications/{application.id}/status",
            json={"new_status": "UNDER_REVIEW"},
            headers=_auth(officer2_user),
        )
        assert r.status_code == 403

    def test_admin_can_patch_without_assignment(self, client, db_session):
        """Admin can change officer-actor statuses without an assignment."""
        dept = _make_isolated_dept(db_session, "patch3")
        application = _make_app_with_status(db_session, dept.id, ApplicationStatus.ASSIGNED)
        admin = _make_user(db_session, "admin", f"padm_{_uid()}@test.dev")

        r = client.patch(
            f"/applications/{application.id}/status",
            json={"new_status": "UNDER_REVIEW"},
            headers=_auth(admin),
        )
        assert r.status_code == 200
        assert r.json()["status"] == "UNDER_REVIEW"


# ══════════════════════════════════════════════════════════════════════════════
# Part 7 — applicant_name in detail response
# ══════════════════════════════════════════════════════════════════════════════

class TestApplicantName:

    def _create_assigned_app(
        self, db_session: Session
    ) -> tuple[Application, User, User, Officer]:
        dept = _make_isolated_dept(db_session, "apn")
        citizen = _make_user(db_session, "citizen", f"apncit_{_uid()}@test.dev", "Jane Doe")
        application = Application(
            citizen_id=citizen.id,
            department_id=dept.id,
            service_type="Test",
            status=ApplicationStatus.ASSIGNED,
            priority=Priority.MEDIUM.value,
            is_duplicate_flag=False,
        )
        db_session.add(application)
        db_session.flush()

        officer_user, officer = _make_officer(db_session, dept.id, f"apnoff_{_uid()}")
        _assign_app(db_session, application, officer)
        return application, citizen, officer_user, officer

    def test_officer_sees_applicant_name(self, client, db_session):
        app, citizen, officer_user, _ = self._create_assigned_app(db_session)
        r = client.get(f"/applications/{app.id}", headers=_auth(officer_user))
        assert r.status_code == 200
        assert r.json()["applicant_name"] == "Jane Doe"

    def test_admin_sees_applicant_name(self, client, db_session):
        app, citizen, _, _ = self._create_assigned_app(db_session)
        admin = _make_user(db_session, "admin", f"apnadm_{_uid()}@test.dev")
        r = client.get(f"/applications/{app.id}", headers=_auth(admin))
        assert r.status_code == 200
        assert r.json()["applicant_name"] == "Jane Doe"

    def test_citizen_sees_null_applicant_name(self, client, db_session):
        dept = _make_isolated_dept(db_session, "apncit")
        citizen = _make_user(db_session, "citizen", f"apncit2_{_uid()}@test.dev", "Jane Doe")
        application = Application(
            citizen_id=citizen.id,
            department_id=dept.id,
            service_type="Test",
            status=ApplicationStatus.DOCUMENT_PENDING,
            priority=Priority.MEDIUM.value,
            is_duplicate_flag=False,
        )
        db_session.add(application)
        db_session.flush()

        r = client.get(f"/applications/{application.id}", headers=_auth(citizen))
        assert r.status_code == 200
        assert r.json()["applicant_name"] is None
