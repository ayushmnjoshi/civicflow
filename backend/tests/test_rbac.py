"""RBAC tests — require_roles() and the shortcut guards.

Strategy
--------
Temporary test routes are registered on a *separate* throwaway APIRouter that
is attached to the real ``app`` only for the duration of the test module, then
removed in a module-scoped teardown.  They are never part of the production
routing table.

The same rollback-per-test database pattern as test_auth.py is used so the DB
is never permanently modified.
"""

from __future__ import annotations

import pytest
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.auth.dependencies import (
    get_current_user,
    require_admin,
    require_citizen,
    require_officer,
    require_staff,
)
from app.auth.security import create_access_token, hash_password
from app.config import settings
from app.database import engine
from app.dependencies import get_db
from app.main import app
from app.models.role import Role
from app.models.user import User


# ── temporary test routes ─────────────────────────────────────────────────────
# Attached to the real app once at module load; never shipped in production.

_test_router = APIRouter(prefix="/test-rbac", tags=["test-rbac-only"])


@_test_router.get("/officer-only", response_model=None)
def _officer_only(user: User = Depends(require_officer)):
    return {"ok": True, "role": "officer"}


@_test_router.get("/admin-only", response_model=None)
def _admin_only(user: User = Depends(require_admin)):
    return {"ok": True, "role": "admin"}


@_test_router.get("/citizen-only", response_model=None)
def _citizen_only(user: User = Depends(require_citizen)):
    return {"ok": True, "role": "citizen"}


@_test_router.get("/staff-only", response_model=None)
def _staff_only(user: User = Depends(require_staff)):
    return {"ok": True, "role": "staff"}


app.include_router(_test_router)


# ── rollback session fixture (same pattern as test_auth.py) ──────────────────

@pytest.fixture(scope="function")
def db_session():
    """Session that rolls back all writes after each test."""
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
    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides.clear()


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_user(db: Session, role_name: str, email: str) -> str:
    """Insert a user with the given role and return a valid JWT for them."""
    role = db.query(Role).filter(Role.name == role_name).first()
    assert role is not None, f"Role '{role_name}' missing — run seed script."

    user = User(
        email=email,
        password_hash=hash_password("password123"),
        role_id=role.id,
        full_name=f"Test {role_name.title()}",
    )
    db.add(user)
    db.flush()

    return create_access_token(
        user_id=user.id,
        secret=settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
        expire_minutes=settings.access_token_expire_minutes,
    )


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ══════════════════════════════════════════════════════════════════════════════
# require_officer guard
# ══════════════════════════════════════════════════════════════════════════════

class TestOfficerGuard:

    def test_officer_gets_200(self, client, db_session):
        token = _make_user(db_session, "officer", "officer@test.dev")
        r = client.get("/test-rbac/officer-only", headers=_auth(token))
        assert r.status_code == 200

    def test_citizen_gets_403(self, client, db_session):
        token = _make_user(db_session, "citizen", "citizen@test.dev")
        r = client.get("/test-rbac/officer-only", headers=_auth(token))
        assert r.status_code == 403

    def test_admin_gets_403_on_officer_only(self, client, db_session):
        """admin must NOT pass an officer-only guard — roles are exact."""
        token = _make_user(db_session, "admin", "admin@test.dev")
        r = client.get("/test-rbac/officer-only", headers=_auth(token))
        assert r.status_code == 403

    def test_missing_token_gets_401(self, client):
        r = client.get("/test-rbac/officer-only")
        assert r.status_code == 401

    def test_invalid_token_gets_401(self, client):
        r = client.get("/test-rbac/officer-only", headers={"Authorization": "Bearer garbage"})
        assert r.status_code == 401


# ══════════════════════════════════════════════════════════════════════════════
# require_admin guard
# ══════════════════════════════════════════════════════════════════════════════

class TestAdminGuard:

    def test_admin_gets_200(self, client, db_session):
        token = _make_user(db_session, "admin", "admin2@test.dev")
        r = client.get("/test-rbac/admin-only", headers=_auth(token))
        assert r.status_code == 200

    def test_citizen_gets_403_on_admin_only(self, client, db_session):
        token = _make_user(db_session, "citizen", "citizen2@test.dev")
        r = client.get("/test-rbac/admin-only", headers=_auth(token))
        assert r.status_code == 403

    def test_missing_token_gets_401(self, client):
        r = client.get("/test-rbac/admin-only")
        assert r.status_code == 401


# ══════════════════════════════════════════════════════════════════════════════
# require_staff guard (officer | dept_head | admin)
# ══════════════════════════════════════════════════════════════════════════════

class TestStaffGuard:

    def test_officer_passes(self, client, db_session):
        token = _make_user(db_session, "officer", "officer3@test.dev")
        assert client.get("/test-rbac/staff-only", headers=_auth(token)).status_code == 200

    def test_dept_head_passes(self, client, db_session):
        token = _make_user(db_session, "dept_head", "dh@test.dev")
        assert client.get("/test-rbac/staff-only", headers=_auth(token)).status_code == 200

    def test_admin_passes(self, client, db_session):
        token = _make_user(db_session, "admin", "admin3@test.dev")
        assert client.get("/test-rbac/staff-only", headers=_auth(token)).status_code == 200

    def test_citizen_gets_403(self, client, db_session):
        token = _make_user(db_session, "citizen", "citizen3@test.dev")
        assert client.get("/test-rbac/staff-only", headers=_auth(token)).status_code == 403

    def test_missing_token_gets_401(self, client):
        assert client.get("/test-rbac/staff-only").status_code == 401


# ══════════════════════════════════════════════════════════════════════════════
# 403 body shape and no stack trace
# ══════════════════════════════════════════════════════════════════════════════

class TestForbiddenShape:

    def test_403_body_has_error_key(self, client, db_session):
        token = _make_user(db_session, "citizen", "citizen4@test.dev")
        body = client.get("/test-rbac/officer-only", headers=_auth(token)).json()
        assert "detail" in body
        # The detail IS the error dict (FastAPI wraps HTTPException.detail as-is)
        assert "error" in body["detail"]
        assert body["detail"]["error"]["code"] == 403

    def test_403_contains_no_stack_trace(self, client, db_session):
        token = _make_user(db_session, "citizen", "citizen5@test.dev")
        body_str = str(
            client.get("/test-rbac/officer-only", headers=_auth(token)).json()
        )
        assert "Traceback" not in body_str
        assert "File \"" not in body_str

    def test_403_has_correct_message(self, client, db_session):
        token = _make_user(db_session, "citizen", "citizen6@test.dev")
        body = client.get("/test-rbac/officer-only", headers=_auth(token)).json()
        msg = body["detail"]["error"]["message"]
        assert "permission" in msg.lower()
