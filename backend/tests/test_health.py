"""Tests for GET /health.

Two cases:
  1. DB reachable   → 200 {"status": "ok", "database": "connected"}
  2. DB not reachable → 503 {"error": {"code": 503, ...}}

The DB-down case is covered with a dependency override that raises
OperationalError without needing to actually stop Postgres.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.main import app
from app.dependencies import get_db


# ── helpers ───────────────────────────────────────────────────────────────────

def _db_down_override():
    """Dependency override: immediately raises a DB connection error."""
    raise OperationalError(
        statement="SELECT 1",
        params=None,
        orig=Exception("Connection refused"),
    )
    yield  # make it a generator so FastAPI treats it as a dependency


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def client():
    """Test client with the real DB dependency (requires live Postgres)."""
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def client_db_down():
    """Test client with get_db overridden to simulate a dead database."""
    app.dependency_overrides[get_db] = _db_down_override
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides.clear()


# ── tests ─────────────────────────────────────────────────────────────────────

class TestHealthSuccess:
    """Health endpoint returns 200 when the DB is reachable."""

    def test_status_code(self, client: TestClient):
        response = client.get("/health")
        assert response.status_code == 200

    def test_body_status_ok(self, client: TestClient):
        body = client.get("/health").json()
        assert body["status"] == "ok"

    def test_body_database_connected(self, client: TestClient):
        body = client.get("/health").json()
        assert body["database"] == "connected"


class TestHealthDBDown:
    """Health endpoint returns 503 when the DB is not reachable."""

    def test_status_code(self, client_db_down: TestClient):
        response = client_db_down.get("/health")
        assert response.status_code == 503

    def test_error_shape(self, client_db_down: TestClient):
        body = client_db_down.get("/health").json()
        assert "error" in body
        assert body["error"]["code"] == 503

    def test_no_stack_trace(self, client_db_down: TestClient):
        """Response must not leak traceback or internal details."""
        body = client_db_down.get("/health").json()
        body_str = str(body)
        assert "Traceback" not in body_str
        assert "Exception" not in body_str
