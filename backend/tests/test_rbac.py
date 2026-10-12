"""RBAC tests — require_roles() and the shortcut guards.

Strategy
--------
Test routes are registered on a *test-local* FastAPI app that is completely
separate from the production ``app``.  The production routing table is never
touched by this test module.

Error shape asserted: {"error": {"code": ..., "message": ...}} at the top level
(the global HTTPException handler in create_app() produces this shape).

The same rollback-per-test database pattern as test_auth.py is used.
"""

from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.auth.dependencies import (
    require_admin,
    require_citizen,
    require_officer,
    require_staff,
)
from app.auth.security import create_access_token, hash_password
from app.config import settings
from app.database import engine
from app.dependencies import get_db
from app.main import create_app
from app.models.role import Role
from app.models.user import User


# ── test-local FastAPI app ────────────────────────────────────────────────────
# Built with create_app() so it has the same exception handlers (401/403 shape).
# Test routes are added here; the production ``app`` is never modified.

_test_app = create_app()


@_test_app.get("/test-rbac/officer-only", response_model=None)
def _officer_only(user: User = Depends(require_officer)):
    return {"ok": True}


@_test_app.get("/test-rbac/admin-only", response_model=None)
def _admin_only(user: User = Depends(require_admin)):
    return {"ok": True}


@_test_app.get("/test-rbac/citizen-only", response_model=None)
def _citizen_only(user: User = Depends(require_citizen)):
    return {"ok": True}


@_test_app.get("/test-rbac/staff-only", response_model=None)
def _staff_only(user: User = Depends(require_staff)):
    return {"ok": True}


# ── rollback session fixture ──────────────────────────────────────────────────

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

    _test_app.dependency_overrides[get_db] = _override
    with TestClient(_test_app, raise_server_exceptions=False) as c:
        yield c
    _test_app.dependency_overrides.clear()


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_user(db: Session, role_name: str, email: str) -> str:
    role = db.query(Role).filter(Role.name == role_name).first()
    assert role is not None, f"Role '{role_name}' not seeded."
    user = User(
        email=email,
        password_hash=hash_password("password123"),
        role_id=role.id,
        full_name=f"Test {role_name}",
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


def _err(r) -> dict:
    """Return the top-level error dict from a response."""
    return r.json()["error"]


# ══════════════════════════════════════════════════════════════════════════════
# Error shape — 403 and 401 use {"error": {"code": ..., "message": ...}}
# ══════════════════════════════════════════════════════════════════════════════

class TestErrorShape:

    def test_403_top_level_error_key(self, client, db_session):
        token = _make_user(db_session, "citizen", "c1@test.dev")
        body = client.get("/test-rbac/officer-only", headers=_auth(token)).json()
        assert "error" in body
        assert "detail" not in body

    def test_403_code_and_message(self, client, db_session):
        token = _make_user(db_session, "citizen", "c2@test.dev")
        err = _err(client.get("/test-rbac/officer-only", headers=_auth(token)))
        assert err["code"] == 403
        assert "permission" in err["message"].lower()

    def test_401_top_level_error_key(self, client):
        body = client.get("/test-rbac/officer-only").json()
        assert "error" in body
        assert "detail" not in body

    def test_401_code(self, client):
        err = _err(client.get("/test-rbac/officer-only"))
        assert err["code"] == 401

    def test_no_stack_trace_in_403(self, client, db_session):
        token = _make_user(db_session, "citizen", "c3@test.dev")
        body_str = str(client.get("/test-rbac/officer-only", headers=_auth(token)).json())
        assert "Traceback" not in body_str
        assert 'File "' not in body_str


# ══════════════════════════════════════════════════════════════════════════════
# require_officer guard
# ══════════════════════════════════════════════════════════════════════════════

class TestOfficerGuard:

    def test_officer_gets_200(self, client, db_session):
        token = _make_user(db_session, "officer", "o1@test.dev")
        assert client.get("/test-rbac/officer-only", headers=_auth(token)).status_code == 200

    def test_citizen_gets_403(self, client, db_session):
        token = _make_user(db_session, "citizen", "c4@test.dev")
        assert client.get("/test-rbac/officer-only", headers=_auth(token)).status_code == 403

    def test_admin_gets_403_on_officer_only(self, client, db_session):
        """admin must NOT pass an officer-only guard — roles are exact."""
        token = _make_user(db_session, "admin", "a1@test.dev")
        assert client.get("/test-rbac/officer-only", headers=_auth(token)).status_code == 403

    def test_missing_token_gets_401(self, client):
        assert client.get("/test-rbac/officer-only").status_code == 401

    def test_invalid_token_gets_401(self, client):
        assert client.get(
            "/test-rbac/officer-only",
            headers={"Authorization": "Bearer garbage"},
        ).status_code == 401


# ══════════════════════════════════════════════════════════════════════════════
# require_admin guard
# ══════════════════════════════════════════════════════════════════════════════

class TestAdminGuard:

    def test_admin_gets_200(self, client, db_session):
        token = _make_user(db_session, "admin", "a2@test.dev")
        assert client.get("/test-rbac/admin-only", headers=_auth(token)).status_code == 200

    def test_citizen_gets_403(self, client, db_session):
        token = _make_user(db_session, "citizen", "c5@test.dev")
        assert client.get("/test-rbac/admin-only", headers=_auth(token)).status_code == 403

    def test_missing_token_gets_401(self, client):
        assert client.get("/test-rbac/admin-only").status_code == 401


# ══════════════════════════════════════════════════════════════════════════════
# require_staff guard (officer | dept_head | admin)
# ══════════════════════════════════════════════════════════════════════════════

class TestStaffGuard:

    def test_officer_passes(self, client, db_session):
        token = _make_user(db_session, "officer", "o2@test.dev")
        assert client.get("/test-rbac/staff-only", headers=_auth(token)).status_code == 200

    def test_dept_head_passes(self, client, db_session):
        token = _make_user(db_session, "dept_head", "dh@test.dev")
        assert client.get("/test-rbac/staff-only", headers=_auth(token)).status_code == 200

    def test_admin_passes(self, client, db_session):
        token = _make_user(db_session, "admin", "a3@test.dev")
        assert client.get("/test-rbac/staff-only", headers=_auth(token)).status_code == 200

    def test_citizen_gets_403(self, client, db_session):
        token = _make_user(db_session, "citizen", "c6@test.dev")
        assert client.get("/test-rbac/staff-only", headers=_auth(token)).status_code == 403

    def test_missing_token_gets_401(self, client):
        assert client.get("/test-rbac/staff-only").status_code == 401
