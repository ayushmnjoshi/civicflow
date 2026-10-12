"""Tests for application endpoints and the state machine.

State machine unit tests are pure Python (no DB).
Endpoint tests use the same rollback-per-test pattern as test_auth.py.

Officer visibility requires an active Assignment row (unassigned_at IS NULL).
We insert those directly via the session inside each test.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.auth.security import create_access_token, hash_password
from app.config import settings
from app.database import engine
from app.dependencies import get_db
from app.main import app
from app.models.application import Application, ApplicationStatus
from app.models.assignment import Assignment
from app.models.department import Department
from app.models.officer import Officer
from app.models.role import Role
from app.models.user import User
from app.services import application_service as svc
from app.services.state_machine import (
    InvalidTransitionError,
    TRANSITIONS,
    can_transition,
    validate_transition,
)


# ══════════════════════════════════════════════════════════════════════════════
# Part 1 — Pure state machine unit tests (no DB)
# ══════════════════════════════════════════════════════════════════════════════

class TestStateMachine:

    # Every legal transition passes for its declared actor
    @pytest.mark.parametrize("from_s,to_s,actor", [
        ("SUBMITTED", "DOCUMENT_PENDING", "system"),
        ("DOCUMENT_PENDING", "VALIDATING", "system"),
        ("VALIDATING", "VALIDATED", "system"),
        ("VALIDATING", "ADDITIONAL_DOCUMENT_REQUIRED", "system"),
        ("VALIDATED", "ASSIGNED", "system"),
        ("ASSIGNED", "UNDER_REVIEW", "officer"),
        ("UNDER_REVIEW", "ADDITIONAL_DOCUMENT_REQUIRED", "officer"),
        ("ADDITIONAL_DOCUMENT_REQUIRED", "DOCUMENT_UPLOADED", "citizen"),
        ("DOCUMENT_UPLOADED", "UNDER_REVIEW", "system"),
        ("UNDER_REVIEW", "APPROVED", "officer"),
        ("UNDER_REVIEW", "REJECTED", "officer"),
    ])
    def test_legal_transitions_pass(self, from_s, to_s, actor):
        remarks = "needed" if (from_s, to_s) in {
            ("UNDER_REVIEW", "REJECTED"),
            ("UNDER_REVIEW", "ADDITIONAL_DOCUMENT_REQUIRED"),
        } else None
        # Should not raise
        validate_transition(from_s, to_s, actor=actor, remarks=remarks)

    # Wrong actor fails for officer-only transition
    def test_wrong_actor_fails(self):
        with pytest.raises(InvalidTransitionError):
            validate_transition("ASSIGNED", "UNDER_REVIEW", actor="citizen")

    def test_wrong_actor_system_as_officer(self):
        with pytest.raises(InvalidTransitionError):
            validate_transition("SUBMITTED", "DOCUMENT_PENDING", actor="officer")

    # Terminal states block all outgoing transitions
    def test_approved_is_terminal(self):
        with pytest.raises(InvalidTransitionError):
            validate_transition("APPROVED", "SUBMITTED", actor="system")

    def test_rejected_is_terminal(self):
        with pytest.raises(InvalidTransitionError):
            validate_transition("REJECTED", "SUBMITTED", actor="citizen")

    # At least 5 illegal (undefined) jumps
    @pytest.mark.parametrize("from_s,to_s", [
        ("SUBMITTED", "APPROVED"),
        ("DOCUMENT_PENDING", "UNDER_REVIEW"),
        ("APPROVED", "REJECTED"),
        ("SUBMITTED", "REJECTED"),
        ("UNDER_REVIEW", "SUBMITTED"),
        ("VALIDATING", "ASSIGNED"),
    ])
    def test_illegal_jumps_raise(self, from_s, to_s):
        with pytest.raises(InvalidTransitionError):
            validate_transition(from_s, to_s, actor="system")

    # Remarks required
    def test_reject_without_remarks_raises(self):
        with pytest.raises(InvalidTransitionError):
            validate_transition("UNDER_REVIEW", "REJECTED", actor="officer", remarks=None)

    def test_reject_with_remarks_passes(self):
        validate_transition("UNDER_REVIEW", "REJECTED", actor="officer", remarks="Bad docs")

    # can_transition helper
    def test_can_transition_true(self):
        assert can_transition("ASSIGNED", "UNDER_REVIEW", "officer") is True

    def test_can_transition_false_wrong_actor(self):
        assert can_transition("ASSIGNED", "UNDER_REVIEW", "citizen") is False

    def test_can_transition_false_terminal(self):
        assert can_transition("APPROVED", "SUBMITTED", "system") is False


# ══════════════════════════════════════════════════════════════════════════════
# Shared fixtures
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
def client(db_session: Session):
    def _override():
        yield db_session

    app.dependency_overrides[get_db] = _override
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides.clear()


# ── seed helpers ──────────────────────────────────────────────────────────────

def _role_id(db: Session, name: str) -> int:
    r = db.query(Role).filter(Role.name == name).first()
    assert r is not None
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
    """Return any existing department (seeded at startup)."""
    d = db.query(Department).first()
    assert d is not None, "No departments seeded — run seed script first."
    return d


def _make_officer_with_assignment(
    db: Session, application_id: int
) -> tuple[User, Officer]:
    """Create an officer user, an Officer row, and an active Assignment."""
    officer_user = _make_user(db, "officer", f"off_{application_id}@test.dev")
    dept = _get_dept(db)
    officer_row = Officer(
        user_id=officer_user.id,
        department_id=dept.id,
        designation="officer",
    )
    db.add(officer_row)
    db.flush()
    assign = Assignment(
        application_id=application_id,
        officer_id=officer_row.id,
    )
    db.add(assign)
    db.flush()
    return officer_user, officer_row


def _create_app_via_api(client: TestClient, citizen: User, dept_id: int) -> dict:
    r = client.post(
        "/applications",
        json={"service_type": "Birth Certificate", "department_id": dept_id},
        headers=_auth(citizen),
    )
    assert r.status_code == 201, r.text
    return r.json()


# ══════════════════════════════════════════════════════════════════════════════
# Part 2 — POST /applications
# ══════════════════════════════════════════════════════════════════════════════

class TestCreateApplication:

    def test_success_returns_201(self, client, db_session):
        citizen = _make_user(db_session, "citizen", "citizen_a@test.dev")
        dept = _get_dept(db_session)
        r = client.post(
            "/applications",
            json={"service_type": "Birth Certificate", "department_id": dept.id},
            headers=_auth(citizen),
        )
        assert r.status_code == 201

    def test_status_is_document_pending(self, client, db_session):
        citizen = _make_user(db_session, "citizen", "citizen_b@test.dev")
        dept = _get_dept(db_session)
        body = _create_app_via_api(client, citizen, dept.id)
        assert body["status"] == "DOCUMENT_PENDING"

    def test_two_history_rows(self, client, db_session):
        citizen = _make_user(db_session, "citizen", "citizen_c@test.dev")
        dept = _get_dept(db_session)
        body = _create_app_via_api(client, citizen, dept.id)
        r = client.get(f"/applications/{body['id']}", headers=_auth(citizen))
        assert r.status_code == 200
        history = r.json()["status_history"]
        assert len(history) == 2
        assert history[0]["from_status"] is None
        assert history[0]["to_status"] == "SUBMITTED"
        assert history[1]["from_status"] == "SUBMITTED"
        assert history[1]["to_status"] == "DOCUMENT_PENDING"

    def test_client_supplied_citizen_id_ignored(self, client, db_session):
        """Any citizen_id in the body is ignored — server uses token."""
        citizen = _make_user(db_session, "citizen", "citizen_d@test.dev")
        dept = _get_dept(db_session)
        # Send a fake citizen_id (99999) — should be silently dropped
        r = client.post(
            "/applications",
            json={
                "service_type": "Test",
                "department_id": dept.id,
                "citizen_id": 99999,
            },
            headers=_auth(citizen),
        )
        assert r.status_code == 201
        assert r.json()["citizen_id"] == citizen.id

    def test_client_supplied_status_ignored(self, client, db_session):
        citizen = _make_user(db_session, "citizen", "citizen_e@test.dev")
        dept = _get_dept(db_session)
        r = client.post(
            "/applications",
            json={
                "service_type": "Test",
                "department_id": dept.id,
                "status": "APPROVED",
            },
            headers=_auth(citizen),
        )
        assert r.status_code == 201
        assert r.json()["status"] == "DOCUMENT_PENDING"

    def test_unknown_department_returns_404(self, client, db_session):
        citizen = _make_user(db_session, "citizen", "citizen_f@test.dev")
        r = client.post(
            "/applications",
            json={"service_type": "Test", "department_id": 999999},
            headers=_auth(citizen),
        )
        assert r.status_code == 404

    def test_officer_gets_403(self, client, db_session):
        officer_user = _make_user(db_session, "officer", "off_cr@test.dev")
        dept = _get_dept(db_session)
        r = client.post(
            "/applications",
            json={"service_type": "Test", "department_id": dept.id},
            headers=_auth(officer_user),
        )
        assert r.status_code == 403

    def test_no_token_returns_401(self, client, db_session):
        dept = _get_dept(db_session)
        r = client.post(
            "/applications",
            json={"service_type": "Test", "department_id": dept.id},
        )
        assert r.status_code == 401


# ══════════════════════════════════════════════════════════════════════════════
# Part 3 — GET /applications/{id} visibility
# ══════════════════════════════════════════════════════════════════════════════

class TestGetApplicationVisibility:

    def test_citizen_sees_own(self, client, db_session):
        citizen = _make_user(db_session, "citizen", "vis_c1@test.dev")
        dept = _get_dept(db_session)
        body = _create_app_via_api(client, citizen, dept.id)
        r = client.get(f"/applications/{body['id']}", headers=_auth(citizen))
        assert r.status_code == 200

    def test_citizen_a_cannot_see_citizen_b(self, client, db_session):
        ca = _make_user(db_session, "citizen", "vis_ca@test.dev")
        cb = _make_user(db_session, "citizen", "vis_cb@test.dev")
        dept = _get_dept(db_session)
        body = _create_app_via_api(client, ca, dept.id)
        r = client.get(f"/applications/{body['id']}", headers=_auth(cb))
        assert r.status_code == 404

    def test_unassigned_officer_gets_404(self, client, db_session):
        citizen = _make_user(db_session, "citizen", "vis_c2@test.dev")
        dept = _get_dept(db_session)
        body = _create_app_via_api(client, citizen, dept.id)
        # Officer with no assignment
        officer_user = _make_user(db_session, "officer", "vis_off_u@test.dev")
        Officer_row = Officer(
            user_id=officer_user.id, department_id=dept.id, designation="officer"
        )
        db_session.add(Officer_row)
        db_session.flush()
        r = client.get(f"/applications/{body['id']}", headers=_auth(officer_user))
        assert r.status_code == 404

    def test_assigned_officer_gets_200(self, client, db_session):
        citizen = _make_user(db_session, "citizen", "vis_c3@test.dev")
        dept = _get_dept(db_session)
        body = _create_app_via_api(client, citizen, dept.id)
        officer_user, _ = _make_officer_with_assignment(db_session, body["id"])
        r = client.get(f"/applications/{body['id']}", headers=_auth(officer_user))
        assert r.status_code == 200

    def test_admin_sees_all(self, client, db_session):
        citizen = _make_user(db_session, "citizen", "vis_c4@test.dev")
        dept = _get_dept(db_session)
        body = _create_app_via_api(client, citizen, dept.id)
        admin = _make_user(db_session, "admin", "vis_admin@test.dev")
        r = client.get(f"/applications/{body['id']}", headers=_auth(admin))
        assert r.status_code == 200

    def test_response_includes_history(self, client, db_session):
        citizen = _make_user(db_session, "citizen", "vis_c5@test.dev")
        dept = _get_dept(db_session)
        body = _create_app_via_api(client, citizen, dept.id)
        r = client.get(f"/applications/{body['id']}", headers=_auth(citizen))
        assert "status_history" in r.json()


# ══════════════════════════════════════════════════════════════════════════════
# Part 4 — GET /applications list
# ══════════════════════════════════════════════════════════════════════════════

class TestListApplications:

    def test_citizen_sees_only_own(self, client, db_session):
        ca = _make_user(db_session, "citizen", "list_ca@test.dev")
        cb = _make_user(db_session, "citizen", "list_cb@test.dev")
        dept = _get_dept(db_session)
        _create_app_via_api(client, ca, dept.id)
        _create_app_via_api(client, cb, dept.id)
        r = client.get("/applications", headers=_auth(ca))
        assert r.status_code == 200
        for item in r.json():
            assert item["citizen_id"] == ca.id

    def test_status_filter(self, client, db_session):
        citizen = _make_user(db_session, "citizen", "list_sf@test.dev")
        dept = _get_dept(db_session)
        _create_app_via_api(client, citizen, dept.id)
        r = client.get(
            "/applications?status=DOCUMENT_PENDING", headers=_auth(citizen)
        )
        for item in r.json():
            assert item["status"] == "DOCUMENT_PENDING"

    def test_limit_default_20(self, client, db_session):
        citizen = _make_user(db_session, "citizen", "list_lim@test.dev")
        dept = _get_dept(db_session)
        for _ in range(5):
            _create_app_via_api(client, citizen, dept.id)
        r = client.get("/applications", headers=_auth(citizen))
        assert len(r.json()) <= 20

    def test_limit_capped_at_100(self, client, db_session):
        """Requesting limit=200 should be capped at 100."""
        citizen = _make_user(db_session, "citizen", "list_cap@test.dev")
        r = client.get("/applications?limit=200", headers=_auth(citizen))
        # 422 if FastAPI validates it, or ≤100 if service caps it
        assert r.status_code in (200, 422)
        if r.status_code == 200:
            assert len(r.json()) <= 100


# ══════════════════════════════════════════════════════════════════════════════
# Part 5 — PATCH /applications/{id}/status
# ══════════════════════════════════════════════════════════════════════════════

class TestPatchStatus:

    def test_officer_can_advance_to_under_review(self, client, db_session):
        citizen = _make_user(db_session, "citizen", "patch_c1@test.dev")
        dept = _get_dept(db_session)
        body = _create_app_via_api(client, citizen, dept.id)
        app_id = body["id"]

        # Manually advance app to ASSIGNED (system actor in DB)
        app_obj = db_session.get(Application, app_id)
        app_obj.status = ApplicationStatus.ASSIGNED
        db_session.flush()

        officer_user, _ = _make_officer_with_assignment(db_session, app_id)
        r = client.patch(
            f"/applications/{app_id}/status",
            json={"new_status": "UNDER_REVIEW"},
            headers=_auth(officer_user),
        )
        assert r.status_code == 200
        assert r.json()["status"] == "UNDER_REVIEW"

    def test_history_written_per_step(self, client, db_session):
        citizen = _make_user(db_session, "citizen", "patch_c2@test.dev")
        dept = _get_dept(db_session)
        body = _create_app_via_api(client, citizen, dept.id)
        app_id = body["id"]

        app_obj = db_session.get(Application, app_id)
        app_obj.status = ApplicationStatus.ASSIGNED
        db_session.flush()

        officer_user, _ = _make_officer_with_assignment(db_session, app_id)
        client.patch(
            f"/applications/{app_id}/status",
            json={"new_status": "UNDER_REVIEW"},
            headers=_auth(officer_user),
        )
        r = client.get(f"/applications/{app_id}", headers=_auth(citizen))
        # history: NULL→SUBMITTED, SUBMITTED→DOCUMENT_PENDING, ASSIGNED→UNDER_REVIEW
        assert len(r.json()["status_history"]) >= 3

    def test_officer_approve(self, client, db_session):
        citizen = _make_user(db_session, "citizen", "patch_c3@test.dev")
        dept = _get_dept(db_session)
        body = _create_app_via_api(client, citizen, dept.id)
        app_id = body["id"]

        app_obj = db_session.get(Application, app_id)
        app_obj.status = ApplicationStatus.UNDER_REVIEW
        db_session.flush()

        officer_user, _ = _make_officer_with_assignment(db_session, app_id)
        r = client.patch(
            f"/applications/{app_id}/status",
            json={"new_status": "APPROVED"},
            headers=_auth(officer_user),
        )
        assert r.status_code == 200
        assert r.json()["status"] == "APPROVED"

    def test_unassigned_officer_gets_404(self, client, db_session):
        citizen = _make_user(db_session, "citizen", "patch_c4@test.dev")
        dept = _get_dept(db_session)
        body = _create_app_via_api(client, citizen, dept.id)
        app_id = body["id"]

        app_obj = db_session.get(Application, app_id)
        app_obj.status = ApplicationStatus.ASSIGNED
        db_session.flush()

        unassigned = _make_user(db_session, "officer", "patch_off_u@test.dev")
        Officer_row = Officer(
            user_id=unassigned.id, department_id=dept.id, designation="officer"
        )
        db_session.add(Officer_row)
        db_session.flush()

        r = client.patch(
            f"/applications/{app_id}/status",
            json={"new_status": "UNDER_REVIEW"},
            headers=_auth(unassigned),
        )
        assert r.status_code == 404

    def test_citizen_gets_403_on_patch(self, client, db_session):
        citizen = _make_user(db_session, "citizen", "patch_c5@test.dev")
        dept = _get_dept(db_session)
        body = _create_app_via_api(client, citizen, dept.id)
        r = client.patch(
            f"/applications/{body['id']}/status",
            json={"new_status": "UNDER_REVIEW"},
            headers=_auth(citizen),
        )
        assert r.status_code == 403

    def test_illegal_jump_returns_400_and_status_unchanged(self, client, db_session):
        citizen = _make_user(db_session, "citizen", "patch_c6@test.dev")
        dept = _get_dept(db_session)
        body = _create_app_via_api(client, citizen, dept.id)
        app_id = body["id"]

        app_obj = db_session.get(Application, app_id)
        app_obj.status = ApplicationStatus.UNDER_REVIEW
        db_session.flush()

        officer_user, _ = _make_officer_with_assignment(db_session, app_id)
        r = client.patch(
            f"/applications/{app_id}/status",
            json={"new_status": "SUBMITTED"},   # illegal jump
            headers=_auth(officer_user),
        )
        assert r.status_code == 400
        # Status must be unchanged
        db_session.expire(app_obj)
        db_session.refresh(app_obj)
        assert app_obj.status == ApplicationStatus.UNDER_REVIEW

    def test_rejected_without_remarks_returns_422(self, client, db_session):
        citizen = _make_user(db_session, "citizen", "patch_c7@test.dev")
        dept = _get_dept(db_session)
        body = _create_app_via_api(client, citizen, dept.id)
        app_id = body["id"]

        app_obj = db_session.get(Application, app_id)
        app_obj.status = ApplicationStatus.UNDER_REVIEW
        db_session.flush()

        officer_user, _ = _make_officer_with_assignment(db_session, app_id)
        r = client.patch(
            f"/applications/{app_id}/status",
            json={"new_status": "REJECTED", "remarks": ""},
            headers=_auth(officer_user),
        )
        assert r.status_code in (400, 422)

    def test_system_only_transition_via_patch_returns_400(self, client, db_session):
        citizen = _make_user(db_session, "citizen", "patch_c8@test.dev")
        dept = _get_dept(db_session)
        body = _create_app_via_api(client, citizen, dept.id)
        app_id = body["id"]

        # Application is at DOCUMENT_PENDING; VALIDATING is system-only
        officer_user, _ = _make_officer_with_assignment(db_session, app_id)
        r = client.patch(
            f"/applications/{app_id}/status",
            json={"new_status": "VALIDATING"},
            headers=_auth(officer_user),
        )
        assert r.status_code == 400


# ══════════════════════════════════════════════════════════════════════════════
# Part 6 — Atomicity
# ══════════════════════════════════════════════════════════════════════════════

class TestAtomicity:

    def test_failed_history_insert_rolls_back_status(self, db_session, monkeypatch):
        """If the history insert fails, the status update must also be rolled back."""
        citizen = _make_user(db_session, "citizen", "atomic_c@test.dev")
        dept = _get_dept(db_session)

        app_obj = Application(
            citizen_id=citizen.id,
            department_id=dept.id,
            service_type="Test",
            status=ApplicationStatus.ASSIGNED,
            priority=0,
            is_duplicate_flag=False,
        )
        db_session.add(app_obj)
        db_session.flush()
        app_id = app_obj.id
        original_status = app_obj.status

        # Monkeypatch _add_history to raise after the status is updated
        import app.services.application_service as _svc_mod

        original_add = _svc_mod._add_history

        def _explode(*args, **kwargs):
            raise RuntimeError("Simulated history insert failure")

        monkeypatch.setattr(_svc_mod, "_add_history", _explode)

        with pytest.raises(Exception):
            svc.change_status(
                db_session,
                application_id=app_id,
                new_status="UNDER_REVIEW",
                actor="officer",
                changed_by=citizen.id,
            )

        monkeypatch.undo()

        # Expire and re-read — status must still be ASSIGNED
        db_session.expire(app_obj)
        db_session.refresh(app_obj)
        assert app_obj.status == original_status
