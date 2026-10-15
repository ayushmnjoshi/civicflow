"""Tests for Prompt 15: pending-check and escalation endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, text
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
from app.models.escalation import Escalation
from app.models.notification import Notification
from app.models.officer import Officer
from app.models.role import Role
from app.models.user import User


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
def client(db_session):
    def _db():
        yield db_session

    app.dependency_overrides[get_db] = _db
    c = TestClient(app, raise_server_exceptions=False)
    yield c
    app.dependency_overrides.pop(get_db, None)


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
        email=email or f"{role}_{_uid()}@p15.test",
        password_hash=hash_password("Test@1234"),
        role_id=_role_id(db, role),
        full_name=f"P15 {role.title()}",
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
    d = Department(name=f"P15Dept {_uid()}", service_type=service_type)
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


def _add_history(db, app_id, from_status, to_status, changed_by, changed_at=None):
    h = ApplicationStatusHistory(
        application_id=app_id,
        from_status=from_status,
        to_status=to_status,
        changed_by=changed_by,
    )
    db.add(h)
    db.flush()
    if changed_at is not None:
        db.execute(
            text("UPDATE application_status_history SET changed_at = :ts WHERE id = :id"),
            {"ts": changed_at, "id": h.id},
        )
        db.flush()
    return h


def _add_notification(db, app_id, recipient_id, status="sent", sent_at=None):
    n = Notification(
        application_id=app_id,
        recipient_id=recipient_id,
        channel="email",
        message="reminder",
        status=status,
        sent_at=sent_at,
    )
    db.add(n)
    db.flush()
    return n


def _add_escalation(db, app_id, escalated_to, resolved_at=None):
    e = Escalation(
        application_id=app_id,
        escalated_to=escalated_to,
        reason="test",
        escalated_at=datetime.now(timezone.utc),
        resolved_at=resolved_at,
    )
    db.add(e)
    db.flush()
    return e


# ══════════════════════════════════════════════════════════════════════════════
# pending-check: token rules
# ══════════════════════════════════════════════════════════════════════════════

class TestPendingCheckTokenRules:
    URL = "/internal/applications/pending-check"

    def test_no_token_401(self, client):
        r = client.get(self.URL, params={"kind": "reminder", "threshold_hours": 24})
        assert r.status_code == 401

    def test_wrong_token_401(self, client):
        r = client.get(self.URL, params={"kind": "reminder", "threshold_hours": 24},
                       headers={"X-Service-Token": "wrong-token"})
        assert r.status_code == 401

    def test_user_jwt_401(self, client, db_session):
        user = _make_user(db_session, "citizen")
        r = client.get(self.URL, params={"kind": "reminder", "threshold_hours": 24},
                       headers=_token(user))
        assert r.status_code == 401


# ══════════════════════════════════════════════════════════════════════════════
# pending-check: parameter validation
# ══════════════════════════════════════════════════════════════════════════════

class TestPendingCheckValidation:
    URL = "/internal/applications/pending-check"

    def test_invalid_kind_422(self, client):
        r = client.get(self.URL, params={"kind": "bogus", "threshold_hours": 24},
                       headers=_svc_token())
        assert r.status_code == 422

    def test_threshold_zero_422(self, client):
        r = client.get(self.URL, params={"kind": "reminder", "threshold_hours": 0},
                       headers=_svc_token())
        assert r.status_code == 422

    def test_threshold_too_large_422(self, client):
        r = client.get(self.URL, params={"kind": "reminder", "threshold_hours": 8761},
                       headers=_svc_token())
        assert r.status_code == 422

    def test_threshold_max_valid(self, client):
        r = client.get(self.URL, params={"kind": "reminder", "threshold_hours": 8760},
                       headers=_svc_token())
        assert r.status_code == 200

    def test_threshold_min_valid(self, client):
        r = client.get(self.URL, params={"kind": "reminder", "threshold_hours": 1},
                       headers=_svc_token())
        assert r.status_code == 200


# ══════════════════════════════════════════════════════════════════════════════
# pending-check: hours from LATEST history row
# ══════════════════════════════════════════════════════════════════════════════

class TestPendingCheckHours:
    URL = "/internal/applications/pending-check"

    def test_old_app_new_status_not_returned(self, client, db_session):
        citizen = _make_user(db_session, "citizen")
        dept = _make_dept(db_session)
        a = _make_app(db_session, citizen.id, dept.id, ApplicationStatus.DOCUMENT_PENDING)
        _add_history(db_session, a.id, None, "DOCUMENT_PENDING", citizen.id,
                     changed_at=datetime.now(timezone.utc) - timedelta(days=5))
        _add_history(db_session, a.id, "DOCUMENT_PENDING", "DOCUMENT_PENDING", citizen.id,
                     changed_at=datetime.now(timezone.utc) - timedelta(hours=1))
        db_session.flush()
        r = client.get(self.URL, params={"kind": "reminder", "threshold_hours": 24},
                       headers=_svc_token())
        assert r.status_code == 200
        assert a.id not in [i["application_id"] for i in r.json()]

    def test_app_with_old_latest_history_returned(self, client, db_session):
        citizen = _make_user(db_session, "citizen")
        dept = _make_dept(db_session)
        a = _make_app(db_session, citizen.id, dept.id, ApplicationStatus.DOCUMENT_PENDING)
        _add_history(db_session, a.id, None, "DOCUMENT_PENDING", citizen.id,
                     changed_at=datetime.now(timezone.utc) - timedelta(hours=30))
        db_session.flush()
        r = client.get(self.URL, params={"kind": "reminder", "threshold_hours": 24},
                       headers=_svc_token())
        assert r.status_code == 200
        assert a.id in [i["application_id"] for i in r.json()]


# ══════════════════════════════════════════════════════════════════════════════
# pending-check: terminal statuses excluded
# ══════════════════════════════════════════════════════════════════════════════

class TestPendingCheckFinalExcluded:
    URL = "/internal/applications/pending-check"

    def test_approved_excluded(self, client, db_session):
        citizen = _make_user(db_session, "citizen")
        dept = _make_dept(db_session)
        a = _make_app(db_session, citizen.id, dept.id, ApplicationStatus.APPROVED)
        _add_history(db_session, a.id, None, "APPROVED", citizen.id,
                     changed_at=datetime.now(timezone.utc) - timedelta(hours=50))
        db_session.flush()
        r = client.get(self.URL, params={"kind": "reminder", "threshold_hours": 1},
                       headers=_svc_token())
        assert a.id not in [i["application_id"] for i in r.json()]

    def test_rejected_excluded(self, client, db_session):
        citizen = _make_user(db_session, "citizen")
        dept = _make_dept(db_session)
        a = _make_app(db_session, citizen.id, dept.id, ApplicationStatus.REJECTED)
        _add_history(db_session, a.id, None, "REJECTED", citizen.id,
                     changed_at=datetime.now(timezone.utc) - timedelta(hours=50))
        db_session.flush()
        r = client.get(self.URL, params={"kind": "reminder", "threshold_hours": 1},
                       headers=_svc_token())
        assert a.id not in [i["application_id"] for i in r.json()]


# ══════════════════════════════════════════════════════════════════════════════
# pending-check: reminder dedupe
# ══════════════════════════════════════════════════════════════════════════════

class TestReminderDedupe:
    URL = "/internal/applications/pending-check"

    def _old_app(self, db_session):
        citizen = _make_user(db_session, "citizen")
        dept = _make_dept(db_session)
        a = _make_app(db_session, citizen.id, dept.id, ApplicationStatus.DOCUMENT_PENDING)
        _add_history(db_session, a.id, None, "DOCUMENT_PENDING", citizen.id,
                     changed_at=datetime.now(timezone.utc) - timedelta(hours=30))
        return a, citizen

    def test_sent_inside_window_suppresses(self, client, db_session):
        a, citizen = self._old_app(db_session)
        _add_notification(db_session, a.id, citizen.id, status="sent",
                          sent_at=datetime.now(timezone.utc) - timedelta(hours=1))
        db_session.flush()
        r = client.get(self.URL, params={"kind": "reminder", "threshold_hours": 24},
                       headers=_svc_token())
        assert a.id not in [i["application_id"] for i in r.json()]

    def test_sent_outside_window_does_not_suppress(self, client, db_session):
        a, citizen = self._old_app(db_session)
        _add_notification(db_session, a.id, citizen.id, status="sent",
                          sent_at=datetime.now(timezone.utc) - timedelta(hours=48))
        db_session.flush()
        r = client.get(self.URL, params={"kind": "reminder", "threshold_hours": 24},
                       headers=_svc_token())
        assert a.id in [i["application_id"] for i in r.json()]

    def test_failed_does_not_suppress(self, client, db_session):
        a, citizen = self._old_app(db_session)
        _add_notification(db_session, a.id, citizen.id, status="failed",
                          sent_at=datetime.now(timezone.utc) - timedelta(hours=1))
        db_session.flush()
        r = client.get(self.URL, params={"kind": "reminder", "threshold_hours": 24},
                       headers=_svc_token())
        assert a.id in [i["application_id"] for i in r.json()]

    def test_pending_does_not_suppress(self, client, db_session):
        a, citizen = self._old_app(db_session)
        _add_notification(db_session, a.id, citizen.id, status="pending", sent_at=None)
        db_session.flush()
        r = client.get(self.URL, params={"kind": "reminder", "threshold_hours": 24},
                       headers=_svc_token())
        assert a.id in [i["application_id"] for i in r.json()]


# ══════════════════════════════════════════════════════════════════════════════
# pending-check: escalation dedupe
# ══════════════════════════════════════════════════════════════════════════════

class TestEscalationDedupe:
    URL = "/internal/applications/pending-check"

    def _old_app(self, db_session):
        citizen = _make_user(db_session, "citizen")
        dept = _make_dept(db_session)
        a = _make_app(db_session, citizen.id, dept.id, ApplicationStatus.DOCUMENT_PENDING)
        _add_history(db_session, a.id, None, "DOCUMENT_PENDING", citizen.id,
                     changed_at=datetime.now(timezone.utc) - timedelta(hours=30))
        return a

    def test_unresolved_escalation_suppresses(self, client, db_session):
        a = self._old_app(db_session)
        target = _make_user(db_session, "admin")
        _add_escalation(db_session, a.id, target.id, resolved_at=None)
        db_session.flush()
        r = client.get(self.URL, params={"kind": "escalation", "threshold_hours": 24},
                       headers=_svc_token())
        assert a.id not in [i["application_id"] for i in r.json()]

    def test_resolved_escalation_does_not_suppress(self, client, db_session):
        a = self._old_app(db_session)
        target = _make_user(db_session, "admin")
        _add_escalation(db_session, a.id, target.id, resolved_at=datetime.now(timezone.utc))
        db_session.flush()
        r = client.get(self.URL, params={"kind": "escalation", "threshold_hours": 24},
                       headers=_svc_token())
        assert a.id in [i["application_id"] for i in r.json()]


# ══════════════════════════════════════════════════════════════════════════════
# pending-check: assigned_officer_user_id field
# ══════════════════════════════════════════════════════════════════════════════

class TestPendingCheckOfficerField:
    URL = "/internal/applications/pending-check"

    def test_assigned_officer_user_id_present(self, client, db_session):
        citizen = _make_user(db_session, "citizen")
        dept = _make_dept(db_session)
        a = _make_app(db_session, citizen.id, dept.id, ApplicationStatus.ASSIGNED)
        _add_history(db_session, a.id, None, "ASSIGNED", citizen.id,
                     changed_at=datetime.now(timezone.utc) - timedelta(hours=30))
        off_user, off = _make_officer(db_session, dept.id)
        assignment = Assignment(application_id=a.id, officer_id=off.id, unassigned_at=None)
        db_session.add(assignment)
        db_session.flush()
        r = client.get(self.URL, params={"kind": "reminder", "threshold_hours": 24},
                       headers=_svc_token())
        assert r.status_code == 200
        items = [i for i in r.json() if i["application_id"] == a.id]
        assert items and items[0]["assigned_officer_user_id"] == off_user.id

    def test_unassigned_null_officer(self, client, db_session):
        citizen = _make_user(db_session, "citizen")
        dept = _make_dept(db_session)
        a = _make_app(db_session, citizen.id, dept.id, ApplicationStatus.DOCUMENT_PENDING)
        _add_history(db_session, a.id, None, "DOCUMENT_PENDING", citizen.id,
                     changed_at=datetime.now(timezone.utc) - timedelta(hours=30))
        db_session.flush()
        r = client.get(self.URL, params={"kind": "reminder", "threshold_hours": 24},
                       headers=_svc_token())
        items = [i for i in r.json() if i["application_id"] == a.id]
        assert items and items[0]["assigned_officer_user_id"] is None


# ══════════════════════════════════════════════════════════════════════════════
# pending-check: ordering and cap
# ══════════════════════════════════════════════════════════════════════════════

class TestPendingCheckOrderingAndCap:
    URL = "/internal/applications/pending-check"

    def test_ordered_by_hours_descending(self, client, db_session):
        citizen = _make_user(db_session, "citizen")
        dept = _make_dept(db_session)
        a1 = _make_app(db_session, citizen.id, dept.id, ApplicationStatus.DOCUMENT_PENDING)
        _add_history(db_session, a1.id, None, "DOCUMENT_PENDING", citizen.id,
                     changed_at=datetime.now(timezone.utc) - timedelta(hours=50))
        a2 = _make_app(db_session, citizen.id, dept.id, ApplicationStatus.DOCUMENT_PENDING)
        _add_history(db_session, a2.id, None, "DOCUMENT_PENDING", citizen.id,
                     changed_at=datetime.now(timezone.utc) - timedelta(hours=30))
        db_session.flush()
        r = client.get(self.URL, params={"kind": "reminder", "threshold_hours": 24},
                       headers=_svc_token())
        assert r.status_code == 200
        items = r.json()
        relevant = [i for i in items if i["application_id"] in (a1.id, a2.id)]
        assert len(relevant) == 2
        assert relevant[0]["application_id"] == a1.id
        assert relevant[0]["hours_in_status"] >= relevant[1]["hours_in_status"]

    def test_cap_constant_is_500(self):
        from app.services import workflow_service
        assert workflow_service._MAX_RESULTS == 500


# ══════════════════════════════════════════════════════════════════════════════
# escalations: token rules
# ══════════════════════════════════════════════════════════════════════════════

class TestEscalationTokenRules:
    URL = "/internal/escalations"

    def test_no_token_401(self, client):
        r = client.post(self.URL, json={"application_id": 1, "reason": "test"})
        assert r.status_code == 401

    def test_wrong_token_401(self, client):
        r = client.post(self.URL, json={"application_id": 1, "reason": "test"},
                        headers={"X-Service-Token": "badtoken"})
        assert r.status_code == 401

    def test_user_jwt_401(self, client, db_session):
        user = _make_user(db_session, "admin")
        r = client.post(self.URL, json={"application_id": 1, "reason": "test"},
                        headers=_token(user))
        assert r.status_code == 401


# ══════════════════════════════════════════════════════════════════════════════
# escalations: input validation + 404/400
# ══════════════════════════════════════════════════════════════════════════════

class TestEscalationValidation:
    URL = "/internal/escalations"

    def test_reason_empty_422(self, client):
        r = client.post(self.URL, json={"application_id": 1, "reason": ""},
                        headers=_svc_token())
        assert r.status_code == 422

    def test_reason_too_long_422(self, client):
        r = client.post(self.URL, json={"application_id": 1, "reason": "x" * 501},
                        headers=_svc_token())
        assert r.status_code == 422

    def test_reason_max_valid(self, client, db_session):
        citizen = _make_user(db_session, "citizen")
        dept = _make_dept(db_session)
        a = _make_app(db_session, citizen.id, dept.id, ApplicationStatus.DOCUMENT_PENDING)
        _make_user(db_session, "admin")
        db_session.flush()
        r = client.post(self.URL, json={"application_id": a.id, "reason": "x" * 500},
                        headers=_svc_token())
        assert r.status_code == 200

    def test_unknown_application_404(self, client):
        r = client.post(self.URL, json={"application_id": 999999999, "reason": "test"},
                        headers=_svc_token())
        assert r.status_code == 404

    def test_approved_app_400(self, client, db_session):
        citizen = _make_user(db_session, "citizen")
        dept = _make_dept(db_session)
        a = _make_app(db_session, citizen.id, dept.id, ApplicationStatus.APPROVED)
        db_session.flush()
        r = client.post(self.URL, json={"application_id": a.id, "reason": "overdue"},
                        headers=_svc_token())
        assert r.status_code == 400

    def test_rejected_app_400(self, client, db_session):
        citizen = _make_user(db_session, "citizen")
        dept = _make_dept(db_session)
        a = _make_app(db_session, citizen.id, dept.id, ApplicationStatus.REJECTED)
        db_session.flush()
        r = client.post(self.URL, json={"application_id": a.id, "reason": "overdue"},
                        headers=_svc_token())
        assert r.status_code == 400


# ══════════════════════════════════════════════════════════════════════════════
# escalations: target selection
# ══════════════════════════════════════════════════════════════════════════════

class TestEscalationTarget:
    URL = "/internal/escalations"

    def test_dept_head_same_dept_chosen(self, client, db_session):
        citizen = _make_user(db_session, "citizen")
        dept = _make_dept(db_session)
        a = _make_app(db_session, citizen.id, dept.id, ApplicationStatus.DOCUMENT_PENDING)
        dh_user, _ = _make_officer(db_session, dept.id, role="dept_head")
        db_session.flush()
        r = client.post(self.URL, json={"application_id": a.id, "reason": "overdue"},
                        headers=_svc_token())
        assert r.status_code == 200
        assert r.json()["escalated_to_user_id"] == dh_user.id

    def test_dept_head_other_dept_ignored(self, client, db_session):
        citizen = _make_user(db_session, "citizen")
        dept = _make_dept(db_session)
        other_dept = _make_dept(db_session)
        a = _make_app(db_session, citizen.id, dept.id, ApplicationStatus.DOCUMENT_PENDING)
        _make_officer(db_session, other_dept.id, role="dept_head")
        _make_user(db_session, "admin")  # ensure at least one admin exists
        db_session.flush()
        # The actual target is the admin with the LOWEST id (may be a seeded admin)
        admin_role_id = db_session.query(Role).filter(Role.name == "admin").first().id
        lowest_admin = (
            db_session.query(User)
            .filter(User.role_id == admin_role_id)
            .order_by(User.id.asc())
            .first()
        )
        r = client.post(self.URL, json={"application_id": a.id, "reason": "overdue"},
                        headers=_svc_token())
        assert r.status_code == 200
        assert r.json()["escalated_to_user_id"] == lowest_admin.id

    def test_fallback_to_admin_no_dept_head(self, client, db_session):
        citizen = _make_user(db_session, "citizen")
        dept = _make_dept(db_session)
        a = _make_app(db_session, citizen.id, dept.id, ApplicationStatus.DOCUMENT_PENDING)
        _make_user(db_session, "admin")  # ensure at least one admin exists
        db_session.flush()
        # The actual target is the admin with the LOWEST id (may be the seeded admin)
        admin_role_id = db_session.query(Role).filter(Role.name == "admin").first().id
        lowest_admin = (
            db_session.query(User)
            .filter(User.role_id == admin_role_id)
            .order_by(User.id.asc())
            .first()
        )
        r = client.post(self.URL, json={"application_id": a.id, "reason": "overdue"},
                        headers=_svc_token())
        assert r.status_code == 200
        assert r.json()["escalated_to_user_id"] == lowest_admin.id

    def test_no_target_409(self, client, db_session):
        citizen = _make_user(db_session, "citizen")
        dept = _make_dept(db_session)
        a = _make_app(db_session, citizen.id, dept.id, ApplicationStatus.DOCUMENT_PENDING)
        db_session.flush()
        with patch("app.services.workflow_service._find_escalation_target", return_value=None):
            r = client.post(self.URL, json={"application_id": a.id, "reason": "overdue"},
                            headers=_svc_token())
        assert r.status_code == 409
        # Custom error shape: {"error": {"code": 409, "message": "..."}}
        assert "no escalation target" in r.json()["error"]["message"]


# ══════════════════════════════════════════════════════════════════════════════
# escalations: idempotent
# ══════════════════════════════════════════════════════════════════════════════

class TestEscalationIdempotent:
    URL = "/internal/escalations"

    def _setup(self, db_session):
        citizen = _make_user(db_session, "citizen")
        dept = _make_dept(db_session)
        a = _make_app(db_session, citizen.id, dept.id, ApplicationStatus.DOCUMENT_PENDING)
        _make_user(db_session, "admin")
        db_session.flush()
        return a

    def test_second_call_same_id(self, client, db_session):
        a = self._setup(db_session)
        r1 = client.post(self.URL, json={"application_id": a.id, "reason": "first"},
                         headers=_svc_token())
        assert r1.status_code == 200
        r2 = client.post(self.URL, json={"application_id": a.id, "reason": "second"},
                         headers=_svc_token())
        assert r2.status_code == 200
        assert r1.json()["escalation_id"] == r2.json()["escalation_id"]

    def test_second_call_no_new_row(self, client, db_session):
        a = self._setup(db_session)
        client.post(self.URL, json={"application_id": a.id, "reason": "first"},
                    headers=_svc_token())
        c1 = db_session.query(Escalation).filter(Escalation.application_id == a.id).count()
        client.post(self.URL, json={"application_id": a.id, "reason": "second"},
                    headers=_svc_token())
        c2 = db_session.query(Escalation).filter(Escalation.application_id == a.id).count()
        assert c1 == c2 == 1


# ══════════════════════════════════════════════════════════════════════════════
# Resolve on final status (Step 3)
# ══════════════════════════════════════════════════════════════════════════════

class TestResolveOnFinal:
    def _app_with_esc(self, db_session, status):
        citizen = _make_user(db_session, "citizen")
        dept = _make_dept(db_session)
        a = _make_app(db_session, citizen.id, dept.id, status)
        admin = _make_user(db_session, "admin")
        esc = _add_escalation(db_session, a.id, admin.id, resolved_at=None)
        db_session.flush()
        return a, esc, admin

    def test_approved_resolves_escalation(self, db_session):
        from app.services.application_service import change_status
        a, esc, admin = self._app_with_esc(db_session, ApplicationStatus.UNDER_REVIEW)
        change_status(db_session, application_id=a.id, new_status="APPROVED",
                      actor="officer", changed_by=admin.id)
        db_session.expire(esc)
        assert db_session.get(Escalation, esc.id).resolved_at is not None

    def test_rejected_resolves_escalation(self, db_session):
        from app.services.application_service import change_status
        a, esc, admin = self._app_with_esc(db_session, ApplicationStatus.UNDER_REVIEW)
        change_status(db_session, application_id=a.id, new_status="REJECTED",
                      actor="officer", changed_by=admin.id, remarks="rejected")
        db_session.expire(esc)
        assert db_session.get(Escalation, esc.id).resolved_at is not None

    def test_other_transition_does_not_resolve(self, db_session):
        from app.services.application_service import change_status
        citizen = _make_user(db_session, "citizen")
        dept = _make_dept(db_session)
        a = _make_app(db_session, citizen.id, dept.id, ApplicationStatus.ASSIGNED)
        admin = _make_user(db_session, "admin")
        off_user, _ = _make_officer(db_session, dept.id)
        esc = _add_escalation(db_session, a.id, admin.id, resolved_at=None)
        db_session.flush()
        change_status(db_session, application_id=a.id, new_status="UNDER_REVIEW",
                      actor="officer", changed_by=off_user.id)
        db_session.expire(esc)
        assert db_session.get(Escalation, esc.id).resolved_at is None


# ══════════════════════════════════════════════════════════════════════════════
# Integration: escalated user can receive notification (Prompt 14 rule)
# ══════════════════════════════════════════════════════════════════════════════

class TestEscalationNotificationIntegration:
    def test_dept_head_can_receive_notification(self, client, db_session):
        citizen = _make_user(db_session, "citizen")
        dept = _make_dept(db_session)
        a = _make_app(db_session, citizen.id, dept.id, ApplicationStatus.DOCUMENT_PENDING)
        dh_user, _ = _make_officer(db_session, dept.id, role="dept_head")
        db_session.flush()
        with patch("app.services.notification_service.LogProvider.send",
                   return_value=("sent", "")):
            r = client.post("/internal/notifications/send", json={
                "recipient_id": dh_user.id, "application_id": a.id,
                "channel": "email", "message": "review escalated application",
            }, headers=_svc_token())
        assert r.status_code == 200
        assert r.json()["status"] == "sent"

    def test_admin_can_receive_notification(self, client, db_session):
        citizen = _make_user(db_session, "citizen")
        dept = _make_dept(db_session)
        a = _make_app(db_session, citizen.id, dept.id, ApplicationStatus.DOCUMENT_PENDING)
        admin = _make_user(db_session, "admin")
        db_session.flush()
        with patch("app.services.notification_service.LogProvider.send",
                   return_value=("sent", "")):
            r = client.post("/internal/notifications/send", json={
                "recipient_id": admin.id, "application_id": a.id,
                "channel": "email", "message": "admin: escalation review needed",
            }, headers=_svc_token())
        assert r.status_code == 200
        assert r.json()["status"] == "sent"
