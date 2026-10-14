"""Tests for Prompt 11 deterministic priority and duplicate rules."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.ai.duplicates import (
    is_duplicate,
    normalize_name,
    normalize_phone,
    normalize_service,
)
from app.ai.priority import WAITING_HIGH_HOURS, decide_priority
from app.auth.security import create_access_token, hash_password
from app.config import settings
from app.constants import Priority
from app.database import engine
from app.dependencies import get_db
from app.main import app
from app.models.application import Application, ApplicationStatus
from app.models.department import Department
from app.models.role import Role
from app.models.user import User
from app.services import application_service as application_svc
from app.services.rule_engine_service import reevaluate_priorities


@pytest.fixture()
def db_session():
    connection = engine.connect()
    transaction = connection.begin()
    nested = connection.begin_nested()
    session = Session(bind=connection)

    @event.listens_for(session, "after_transaction_end")
    def restart_savepoint(_session, trans):
        nonlocal nested
        if trans.nested and not trans.is_active:
            nested = connection.begin_nested()

    yield session
    session.close()
    transaction.rollback()
    connection.close()


@pytest.fixture()
def client(db_session):
    def _db():
        yield db_session

    app.dependency_overrides[get_db] = _db
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _make_user(
    db: Session, role: str, email: str, full_name: str = "Raj Kumar", phone: str | None = None
) -> User:
    role_row = db.query(Role).filter(Role.name == role).first()
    assert role_row is not None
    user = User(
        email=email,
        password_hash=hash_password("Test@1234"),
        role_id=role_row.id,
        full_name=full_name,
        phone=phone,
    )
    db.add(user)
    db.flush()
    return user


def _auth(user: User) -> dict[str, str]:
    token = create_access_token(
        user_id=user.id,
        secret=settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
        expire_minutes=settings.access_token_expire_minutes,
    )
    return {"Authorization": f"Bearer {token}"}


def _dept(db: Session) -> Department:
    department = db.query(Department).filter(Department.service_type == "civic_services").first()
    assert department is not None
    return department


def _app(app_id, citizen_id, department_id, service_type="Birth Certificate", status="DOCUMENT_PENDING"):
    return SimpleNamespace(
        id=app_id,
        citizen_id=citizen_id,
        department_id=department_id,
        service_type=service_type,
        status=status,
    )


def _user(user_id, name="Raj Kumar", phone="+91 98765 43210"):
    return SimpleNamespace(id=user_id, full_name=name, phone=phone)


class TestPriorityRules:
    def test_urgent_is_high(self):
        assert decide_priority(urgent=True, waiting_hours=0, department_service_type="civic_services") == (
            Priority.HIGH,
            "urgent_flag",
        )

    @pytest.mark.parametrize("hours", [WAITING_HIGH_HOURS, WAITING_HIGH_HOURS + 1])
    def test_waiting_threshold_is_high(self, hours):
        assert decide_priority(urgent=False, waiting_hours=hours, department_service_type="unknown")[0] is Priority.HIGH

    def test_standard_department_category_is_medium(self):
        assert decide_priority(urgent=False, waiting_hours=0, department_service_type="civic_services")[0] is Priority.MEDIUM

    def test_unknown_category_is_low(self):
        assert decide_priority(urgent=False, waiting_hours=0, department_service_type="unknown")[0] is Priority.LOW

    def test_urgent_wins_over_category(self):
        assert decide_priority(urgent=True, waiting_hours=0, department_service_type="unknown") == (
            Priority.HIGH,
            "urgent_flag",
        )


class TestDuplicateRules:
    def test_normalizers_ignore_name_order_punctuation_and_phone_formatting(self):
        assert normalize_name("Raj Kumar") == normalize_name("kumar, RAJ")
        assert normalize_phone("+91 98765 43210") == "9876543210"
        assert normalize_service("  Birth   Certificate ") == "birth certificate"

    def test_same_citizen_and_service_is_duplicate(self):
        assert is_duplicate(_app(2, 1, 1), _user(1), _app(1, 1, 1), _user(1))

    def test_matching_name_and_phone_across_citizens_is_duplicate(self):
        assert is_duplicate(_app(2, 2, 1), _user(2), _app(1, 1, 1), _user(1, "kumar, RAJ"))

    @pytest.mark.parametrize("name,phone", [("Raj Kumar", "9876543211"), ("Raj Kumar", None)])
    def test_different_or_missing_phone_is_not_duplicate(self, name, phone):
        assert not is_duplicate(_app(2, 2, 1), _user(2), _app(1, 1, 1), _user(1, name, phone))

    @pytest.mark.parametrize(
        "candidate",
        [_app(1, 1, 1, "Death Certificate"), _app(1, 1, 1, status="REJECTED"), _app(2, 1, 1)],
    )
    def test_nonmatching_rejected_or_self_candidate_is_not_duplicate(self, candidate):
        assert not is_duplicate(_app(2, 1, 1), _user(1), candidate, _user(1))


class TestApplicationRuleIntegration:
    def _create(self, client, citizen, department_id, *, urgent=False):
        return client.post(
            "/applications",
            json={"service_type": "Birth Certificate", "department_id": department_id, "urgent": urgent},
            headers=_auth(citizen),
        )

    def test_default_and_urgent_priority(self, client, db_session):
        citizen = _make_user(db_session, "citizen", "prompt11-priority@test.dev")
        department = _dept(db_session)
        assert self._create(client, citizen, department.id).json()["priority"] == "MEDIUM"
        assert self._create(client, citizen, department.id, urgent=True).json()["priority"] == "HIGH"

    def test_duplicate_is_staff_visible_and_citizen_hidden(self, client, db_session):
        citizen = _make_user(db_session, "citizen", "prompt11-duplicate@test.dev")
        admin = _make_user(db_session, "admin", "prompt11-admin@test.dev")
        department = _dept(db_session)
        self._create(client, citizen, department.id)
        second = self._create(client, citizen, department.id)
        assert second.status_code == 201
        application_id = second.json()["id"]
        assert second.json()["is_duplicate_flag"] is None
        assert client.get(f"/applications/{application_id}", headers=_auth(citizen)).json()["is_duplicate_flag"] is None
        assert client.get(f"/applications/{application_id}", headers=_auth(admin)).json()["is_duplicate_flag"] is True

    def test_rule_failure_keeps_defaults_and_creates_application(self, client, db_session, monkeypatch, caplog):
        citizen = _make_user(db_session, "citizen", "prompt11-failure@test.dev")
        department = _dept(db_session)

        def broken_rule_engine(*_args, **_kwargs):
            raise RuntimeError("test rule failure")

        monkeypatch.setattr(application_svc, "apply_rules_on_create", broken_rule_engine)
        response = self._create(client, citizen, department.id)
        assert response.status_code == 201
        assert response.json()["priority"] == "MEDIUM"
        created = db_session.get(Application, response.json()["id"])
        assert created.is_duplicate_flag is False
        assert "rule_engine_error" in caplog.text


class TestPriorityReevaluation:
    def test_only_old_nonterminal_lower_priority_apps_are_raised(self, db_session):
        citizen = _make_user(db_session, "citizen", "prompt11-recheck@test.dev")
        department = _dept(db_session)
        old = Application(citizen_id=citizen.id, department_id=department.id, service_type="Test", status=ApplicationStatus.DOCUMENT_PENDING, priority=Priority.LOW.value, is_duplicate_flag=False)
        recent = Application(citizen_id=citizen.id, department_id=department.id, service_type="Test", status=ApplicationStatus.DOCUMENT_PENDING, priority=Priority.MEDIUM.value, is_duplicate_flag=False)
        approved = Application(citizen_id=citizen.id, department_id=department.id, service_type="Test", status=ApplicationStatus.APPROVED, priority=Priority.LOW.value, is_duplicate_flag=False)
        rejected = Application(citizen_id=citizen.id, department_id=department.id, service_type="Test", status=ApplicationStatus.REJECTED, priority=Priority.LOW.value, is_duplicate_flag=False)
        db_session.add_all([old, recent, approved, rejected])
        db_session.flush()
        old.created_at = datetime.now(timezone.utc) - timedelta(hours=WAITING_HIGH_HOURS)
        approved.created_at = old.created_at
        rejected.created_at = old.created_at
        db_session.flush()
        assert reevaluate_priorities(db_session) == 1
        assert old.priority == Priority.HIGH.value
        assert recent.priority == Priority.MEDIUM.value
        assert approved.priority == Priority.LOW.value
        assert rejected.priority == Priority.LOW.value

    def test_internal_route_requires_service_token_and_returns_count(self, client, db_session):
        citizen = _make_user(db_session, "citizen", "prompt11-internal@test.dev")
        assert client.post("/internal/priority/reevaluate").status_code == 401
        assert client.post("/internal/priority/reevaluate", headers={"X-Service-Token": "wrong"}).status_code == 401
        assert client.post("/internal/priority/reevaluate", headers=_auth(citizen)).status_code == 401
        response = client.post(
            "/internal/priority/reevaluate",
            headers={"X-Service-Token": settings.internal_service_token},
        )
        assert response.status_code == 200
        assert isinstance(response.json()["updated"], int)
