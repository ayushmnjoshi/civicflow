"""Tests for auth endpoints (POST /auth/register, POST /auth/login, GET /auth/me).

Each test runs inside a database transaction that is rolled back at the end,
so the real database is never permanently modified.

SQLAlchemy 2.x rollback pattern used:
  - engine.connect() opens one connection
  - connection.begin() starts an outer transaction (never committed)
  - connection.begin_nested() creates a SAVEPOINT before each session operation
  - after_transaction_end event restarts the SAVEPOINT whenever the session
    "commits" (which only releases the SAVEPOINT, not the outer transaction)
  - transaction.rollback() at teardown undoes everything
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.auth.security import decode_access_token, hash_password
from app.config import settings
from app.database import engine, SessionLocal
from app.dependencies import get_db
from app.main import app
from app.models.user import User


# ── rollback session fixture ──────────────────────────────────────────────────

@pytest.fixture(scope="function")
def db_session():
    """Session that rolls back all writes after each test."""
    connection = engine.connect()
    transaction = connection.begin()
    nested = connection.begin_nested()          # SAVEPOINT

    session = Session(bind=connection)

    # After the session releases a SAVEPOINT on "commit", immediately open a
    # new one so subsequent operations in the same test still have a savepoint.
    @event.listens_for(session, "after_transaction_end")
    def restart_savepoint(sess, trans):
        nonlocal nested
        if trans.nested and not trans.is_active:
            nested = connection.begin_nested()

    yield session

    session.close()
    transaction.rollback()   # rolls back everything — DB stays clean
    connection.close()


@pytest.fixture(scope="function")
def client(db_session: Session):
    """TestClient whose get_db dependency uses the rollback session."""
    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides.clear()


# ── shared helpers ────────────────────────────────────────────────────────────

_REGISTER_PAYLOAD = {
    "email": "alice@example.com",
    "password": "securepass1",
    "full_name": "Alice Test",
    "phone": "9999999999",
}


def _register(client: TestClient, payload: dict | None = None) -> dict:
    return client.post("/auth/register", json=payload or _REGISTER_PAYLOAD).json()


def _login(client: TestClient, email: str = "alice@example.com", password: str = "securepass1") -> dict:
    return client.post("/auth/login", json={"email": email, "password": password}).json()


def _expired_token() -> str:
    """Create a structurally valid token that is already expired."""
    payload = {
        "sub": "999",
        "iat": datetime.now(timezone.utc) - timedelta(hours=2),
        "exp": datetime.now(timezone.utc) - timedelta(hours=1),
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


# ══════════════════════════════════════════════════════════════════════════════
# POST /auth/register
# ══════════════════════════════════════════════════════════════════════════════

class TestRegister:

    def test_success_returns_201(self, client):
        r = client.post("/auth/register", json=_REGISTER_PAYLOAD)
        assert r.status_code == 201

    def test_success_body_shape(self, client):
        body = client.post("/auth/register", json=_REGISTER_PAYLOAD).json()
        assert "user_id" in body
        assert body["email"] == "alice@example.com"
        assert body["full_name"] == "Alice Test"
        assert body["role"] == "citizen"

    def test_no_password_hash_in_response(self, client):
        body = client.post("/auth/register", json=_REGISTER_PAYLOAD).json()
        assert "password_hash" not in body
        assert "password" not in body

    def test_duplicate_email_returns_409(self, client):
        client.post("/auth/register", json=_REGISTER_PAYLOAD)   # first
        r = client.post("/auth/register", json=_REGISTER_PAYLOAD)  # duplicate
        assert r.status_code == 409

    def test_password_too_short_returns_422(self, client):
        bad = {**_REGISTER_PAYLOAD, "password": "short"}
        r = client.post("/auth/register", json=bad)
        assert r.status_code == 422

    def test_role_field_ignored_user_is_citizen(self, client):
        """Sending role='admin' in the body must be silently ignored."""
        payload_with_role = {**_REGISTER_PAYLOAD, "role": "admin"}
        body = client.post("/auth/register", json=payload_with_role).json()
        assert body["role"] == "citizen"

    def test_email_stored_lowercase(self, client, db_session: Session):
        payload = {**_REGISTER_PAYLOAD, "email": "ALICE@EXAMPLE.COM"}
        body = client.post("/auth/register", json=payload).json()
        user = db_session.get(User, body["user_id"])
        assert user is not None
        assert user.email == "alice@example.com"

    def test_password_hash_not_equal_to_plain(self, client, db_session: Session):
        body = client.post("/auth/register", json=_REGISTER_PAYLOAD).json()
        user = db_session.get(User, body["user_id"])
        # Refresh to pick up the committed row
        db_session.refresh(user)
        assert user.password_hash != _REGISTER_PAYLOAD["password"]
        assert len(user.password_hash) > 20   # bcrypt hash is long


# ══════════════════════════════════════════════════════════════════════════════
# POST /auth/login
# ══════════════════════════════════════════════════════════════════════════════

class TestLogin:

    def test_success_returns_200(self, client):
        _register(client)
        r = client.post("/auth/login", json={"email": "alice@example.com", "password": "securepass1"})
        assert r.status_code == 200

    def test_success_body_has_token_and_role(self, client):
        _register(client)
        body = _login(client)
        assert "access_token" in body
        assert body["token_type"] == "bearer"
        assert body["role"] == "citizen"

    def test_wrong_password_returns_401(self, client):
        _register(client)
        r = client.post("/auth/login", json={"email": "alice@example.com", "password": "wrongpassword"})
        assert r.status_code == 401

    def test_unknown_email_returns_401(self, client):
        r = client.post("/auth/login", json={"email": "nobody@example.com", "password": "securepass1"})
        assert r.status_code == 401

    def test_wrong_password_and_unknown_email_same_message(self, client):
        """Both failures must return the identical detail string (no enumeration)."""
        _register(client)
        r_wrong_pw = client.post(
            "/auth/login",
            json={"email": "alice@example.com", "password": "wrong"},
        )
        r_unknown = client.post(
            "/auth/login",
            json={"email": "nobody@example.com", "password": "securepass1"},
        )
        assert r_wrong_pw.status_code == r_unknown.status_code == 401
        assert (
            r_wrong_pw.json()["error"]["message"]
            == r_unknown.json()["error"]["message"]
        )

    def test_no_password_hash_in_response(self, client):
        _register(client)
        body = _login(client)
        body_str = str(body)
        assert "password_hash" not in body_str
        assert "password" not in body_str


# ══════════════════════════════════════════════════════════════════════════════
# GET /auth/me
# ══════════════════════════════════════════════════════════════════════════════

class TestMe:

    def test_success_returns_200(self, client):
        _register(client)
        token = _login(client)["access_token"]
        r = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200

    def test_success_body_shape(self, client):
        _register(client)
        token = _login(client)["access_token"]
        body = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"}).json()
        assert body["email"] == "alice@example.com"
        assert body["full_name"] == "Alice Test"
        assert body["role"] == "citizen"
        assert "id" in body
        assert "password_hash" not in body

    def test_no_token_returns_401(self, client):
        r = client.get("/auth/me")
        assert r.status_code == 401

    def test_garbage_token_returns_401(self, client):
        r = client.get("/auth/me", headers={"Authorization": "Bearer not.a.token"})
        assert r.status_code == 401

    def test_expired_token_returns_401(self, client):
        r = client.get("/auth/me", headers={"Authorization": f"Bearer {_expired_token()}"})
        assert r.status_code == 401
