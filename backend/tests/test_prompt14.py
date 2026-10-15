"""Tests for Prompt 14: notification service, send/list endpoints, event webhook."""

from __future__ import annotations

import http.server
import json
import queue
import socket
import threading
import time
import uuid
from unittest.mock import MagicMock, patch

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
from app.models.assignment import Assignment
from app.models.department import Department
from app.models.notification import Notification
from app.models.officer import Officer
from app.models.role import Role
from app.models.user import User
from app.services.notification_service import (
    LogProvider,
    SmtpProvider,
    get_provider,
)


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

def _uid() -> str:
    return uuid.uuid4().hex[:10]


def _role_id(db: Session, name: str) -> int:
    r = db.query(Role).filter(Role.name == name).first()
    assert r is not None, f"role '{name}' not found"
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


def _service_token_header() -> dict:
    return {"X-Service-Token": settings.internal_service_token}


def _make_dept(db: Session) -> Department:
    dept = Department(name=f"P14Dept {_uid()}", service_type="civic_services")
    db.add(dept)
    db.flush()
    return dept


def _make_officer(db: Session, dept_id: int) -> tuple[User, Officer]:
    u = _make_user(db, "officer", f"off_{_uid()}@p14.test")
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


def _make_notification(
    db: Session,
    app_id: int,
    recipient_id: int,
    message: str = "test msg",
    status: str = "sent",
) -> Notification:
    n = Notification(
        application_id=app_id,
        recipient_id=recipient_id,
        channel="email",
        message=message,
        status=status,
    )
    db.add(n)
    db.flush()
    return n


# ══════════════════════════════════════════════════════════════════════════════
# STEP 2: Provider unit tests
# ══════════════════════════════════════════════════════════════════════════════

class TestLogProvider:
    def test_returns_sent(self):
        prov = LogProvider()
        status, _ = prov.send(
            notification_id=1,
            application_id=42,
            recipient_email="u@example.com",
            message="hello",
        )
        assert status == "sent"

    def test_does_not_log_address_or_body(self, caplog):
        import logging
        prov = LogProvider()
        with caplog.at_level(logging.INFO, logger="app.services.notification_service"):
            prov.send(
                notification_id=99,
                application_id=1,
                recipient_email="secret@example.com",
                message="secret body",
            )
        combined = " ".join(caplog.messages)
        assert "secret@example.com" not in combined
        assert "secret body" not in combined
        assert "99" in combined  # notification_id IS logged


class TestSmtpProvider:
    def _smtp_settings(self, monkeypatch, host="mail.example.com", from_addr="no-reply@example.com",
                        username="", password="", starttls=True):
        monkeypatch.setattr(settings, "smtp_host", host)
        monkeypatch.setattr(settings, "smtp_from", from_addr)
        monkeypatch.setattr(settings, "smtp_username", username)
        monkeypatch.setattr(settings, "smtp_password", password)
        monkeypatch.setattr(settings, "smtp_starttls", starttls)
        monkeypatch.setattr(settings, "smtp_port", 587)

    def test_success_with_starttls_and_login(self, monkeypatch):
        self._smtp_settings(monkeypatch, username="user", password="pass", starttls=True)
        with patch("smtplib.SMTP") as mock_smtp_cls:
            mock_smtp = MagicMock()
            mock_smtp_cls.return_value.__enter__ = lambda s: mock_smtp
            mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)
            prov = SmtpProvider()
            status, _ = prov.send(1, 10, "r@example.com", "hello")
        assert status == "sent"
        mock_smtp.starttls.assert_called_once()
        mock_smtp.login.assert_called_once_with("user", "pass")

    def test_starttls_not_called_when_disabled(self, monkeypatch):
        self._smtp_settings(monkeypatch, starttls=False, username="u", password="p")
        with patch("smtplib.SMTP") as mock_smtp_cls:
            mock_smtp = MagicMock()
            mock_smtp_cls.return_value.__enter__ = lambda s: mock_smtp
            mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)
            prov = SmtpProvider()
            status, _ = prov.send(2, 10, "r@example.com", "hello")
        assert status == "sent"
        mock_smtp.starttls.assert_not_called()

    def test_login_not_called_without_username(self, monkeypatch):
        self._smtp_settings(monkeypatch, username="", starttls=True)
        with patch("smtplib.SMTP") as mock_smtp_cls:
            mock_smtp = MagicMock()
            mock_smtp_cls.return_value.__enter__ = lambda s: mock_smtp
            mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)
            prov = SmtpProvider()
            prov.send(3, 10, "r@example.com", "hello")
        mock_smtp.login.assert_not_called()

    def test_header_injection_does_not_create_bcc(self, monkeypatch):
        """A message body containing '\\r\\nBcc: x@y' must not create a Bcc header."""
        self._smtp_settings(monkeypatch, username="", starttls=False)
        captured: list = []

        def fake_send_message(msg):
            captured.append(msg)

        with patch("smtplib.SMTP") as mock_smtp_cls:
            mock_smtp = MagicMock()
            mock_smtp.send_message.side_effect = fake_send_message
            mock_smtp_cls.return_value.__enter__ = lambda s: mock_smtp
            mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)
            prov = SmtpProvider()
            prov.send(4, 10, "r@example.com", "legit\r\nBcc: x@y")

        assert len(captured) == 1
        msg = captured[0]
        # EmailMessage properly encodes body; Bcc header must NOT exist
        assert msg["Bcc"] is None

    def test_missing_host_fails_closed(self, monkeypatch):
        monkeypatch.setattr(settings, "smtp_host", "")
        monkeypatch.setattr(settings, "smtp_from", "no-reply@example.com")
        prov = SmtpProvider()
        status, reason = prov.send(5, 10, "r@example.com", "hello")
        assert status == "failed"
        assert reason == "smtp_not_configured"

    def test_missing_from_fails_closed(self, monkeypatch):
        monkeypatch.setattr(settings, "smtp_host", "mail.example.com")
        monkeypatch.setattr(settings, "smtp_from", "")
        prov = SmtpProvider()
        status, reason = prov.send(6, 10, "r@example.com", "hello")
        assert status == "failed"
        assert reason == "smtp_not_configured"

    def test_smtp_exception_fails_closed(self, monkeypatch):
        self._smtp_settings(monkeypatch)
        with patch("smtplib.SMTP", side_effect=ConnectionRefusedError("refused")):
            prov = SmtpProvider()
            status, reason = prov.send(7, 10, "r@example.com", "hello")
        assert status == "failed"
        assert "smtp_send_failed" in reason

    def test_subject_contains_application_id(self, monkeypatch):
        self._smtp_settings(monkeypatch, username="", starttls=False)
        captured: list = []

        def fake_send_message(msg):
            captured.append(msg)

        with patch("smtplib.SMTP") as mock_smtp_cls:
            mock_smtp = MagicMock()
            mock_smtp.send_message.side_effect = fake_send_message
            mock_smtp_cls.return_value.__enter__ = lambda s: mock_smtp
            mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)
            prov = SmtpProvider()
            prov.send(8, 9999, "r@example.com", "hello")

        assert len(captured) == 1
        assert "9999" in captured[0]["Subject"]


class TestGetProvider:
    def test_log_provider_is_default(self, monkeypatch):
        monkeypatch.setattr(settings, "notify_provider", "log")
        from app.services.notification_service import get_provider, LogProvider
        assert isinstance(get_provider(), LogProvider)

    def test_smtp_provider_when_configured(self, monkeypatch):
        monkeypatch.setattr(settings, "notify_provider", "smtp")
        from app.services.notification_service import get_provider, SmtpProvider
        assert isinstance(get_provider(), SmtpProvider)

    def test_unknown_provider_fails_closed(self, monkeypatch):
        monkeypatch.setattr(settings, "notify_provider", "carrier_pigeon")
        from app.services.notification_service import get_provider, _UnknownProvider
        prov = get_provider()
        assert isinstance(prov, _UnknownProvider)
        status, reason = prov.send(10, 1, "r@example.com", "hello")
        assert status == "failed"
        assert reason == "unknown_provider"


# ══════════════════════════════════════════════════════════════════════════════
# STEP 3: POST /internal/notifications/send
# ══════════════════════════════════════════════════════════════════════════════

class TestNotificationSendEndpoint:
    def _body(self, recipient_id: int, app_id: int, message: str = "Hello!") -> dict:
        return {
            "recipient_id": recipient_id,
            "application_id": app_id,
            "channel": "email",
            "message": message,
        }

    # ── token rules ─────────────────────────────────────────────────────────

    def test_no_token_401(self, client, db_session):
        dept = _make_dept(db_session)
        citizen = _make_user(db_session, "citizen", f"cit_{_uid()}@p14.test")
        a = _make_app(db_session, citizen.id, dept.id)
        r = client.post("/internal/notifications/send", json=self._body(citizen.id, a.id))
        assert r.status_code == 401

    def test_wrong_token_401(self, client, db_session):
        dept = _make_dept(db_session)
        citizen = _make_user(db_session, "citizen", f"cit_{_uid()}@p14.test")
        a = _make_app(db_session, citizen.id, dept.id)
        r = client.post(
            "/internal/notifications/send",
            json=self._body(citizen.id, a.id),
            headers={"X-Service-Token": "wrong-token"},
        )
        assert r.status_code == 401

    def test_user_jwt_401(self, client, db_session):
        dept = _make_dept(db_session)
        citizen = _make_user(db_session, "citizen", f"cit_{_uid()}@p14.test")
        a = _make_app(db_session, citizen.id, dept.id)
        r = client.post(
            "/internal/notifications/send",
            json=self._body(citizen.id, a.id),
            headers=_token(citizen),
        )
        assert r.status_code == 401

    # ── channel validation ───────────────────────────────────────────────────

    def test_channel_not_email_422(self, client, db_session):
        dept = _make_dept(db_session)
        citizen = _make_user(db_session, "citizen", f"cit_{_uid()}@p14.test")
        a = _make_app(db_session, citizen.id, dept.id)
        body = {"recipient_id": citizen.id, "application_id": a.id, "channel": "sms", "message": "Hi"}
        r = client.post("/internal/notifications/send", json=body, headers=_service_token_header())
        assert r.status_code == 422

    # ── 404 cases ────────────────────────────────────────────────────────────

    def test_unknown_recipient_404(self, client, db_session):
        dept = _make_dept(db_session)
        citizen = _make_user(db_session, "citizen", f"cit_{_uid()}@p14.test")
        a = _make_app(db_session, citizen.id, dept.id)
        r = client.post(
            "/internal/notifications/send",
            json=self._body(99999, a.id),
            headers=_service_token_header(),
        )
        assert r.status_code == 404

    def test_unknown_application_404(self, client, db_session):
        dept = _make_dept(db_session)
        citizen = _make_user(db_session, "citizen", f"cit_{_uid()}@p14.test")
        r = client.post(
            "/internal/notifications/send",
            json=self._body(citizen.id, 99999),
            headers=_service_token_header(),
        )
        assert r.status_code == 404

    # ── authorization ─────────────────────────────────────────────────────────

    def test_unrelated_citizen_403(self, client, db_session):
        """A citizen who is not the applicant must get 403."""
        dept = _make_dept(db_session)
        citizen = _make_user(db_session, "citizen", f"cit_{_uid()}@p14.test")
        other = _make_user(db_session, "citizen", f"cit_{_uid()}@p14.test")
        a = _make_app(db_session, citizen.id, dept.id)
        r = client.post(
            "/internal/notifications/send",
            json=self._body(other.id, a.id),
            headers=_service_token_header(),
        )
        assert r.status_code == 403

    def test_unassigned_officer_403(self, client, db_session):
        dept = _make_dept(db_session)
        citizen = _make_user(db_session, "citizen", f"cit_{_uid()}@p14.test")
        a = _make_app(db_session, citizen.id, dept.id)
        off_user, _ = _make_officer(db_session, dept.id)  # NOT assigned
        r = client.post(
            "/internal/notifications/send",
            json=self._body(off_user.id, a.id),
            headers=_service_token_header(),
        )
        assert r.status_code == 403

    def test_citizen_allowed(self, client, db_session, monkeypatch):
        monkeypatch.setattr(settings, "notify_provider", "log")
        dept = _make_dept(db_session)
        citizen = _make_user(db_session, "citizen", f"cit_{_uid()}@p14.test")
        a = _make_app(db_session, citizen.id, dept.id)
        r = client.post(
            "/internal/notifications/send",
            json=self._body(citizen.id, a.id, "Your application has been received."),
            headers=_service_token_header(),
        )
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "sent"
        assert "notification_id" in data

    def test_assigned_officer_allowed(self, client, db_session, monkeypatch):
        monkeypatch.setattr(settings, "notify_provider", "log")
        dept = _make_dept(db_session)
        citizen = _make_user(db_session, "citizen", f"cit_{_uid()}@p14.test")
        a = _make_app(db_session, citizen.id, dept.id)
        off_user, officer = _make_officer(db_session, dept.id)
        db_session.add(Assignment(application_id=a.id, officer_id=officer.id))
        db_session.flush()
        r = client.post(
            "/internal/notifications/send",
            json=self._body(off_user.id, a.id, "Task assigned to you."),
            headers=_service_token_header(),
        )
        assert r.status_code == 200
        assert r.json()["status"] == "sent"

    def test_admin_allowed(self, client, db_session, monkeypatch):
        monkeypatch.setattr(settings, "notify_provider", "log")
        dept = _make_dept(db_session)
        citizen = _make_user(db_session, "citizen", f"cit_{_uid()}@p14.test")
        admin = _make_user(db_session, "admin", f"adm_{_uid()}@p14.test")
        a = _make_app(db_session, citizen.id, dept.id)
        r = client.post(
            "/internal/notifications/send",
            json=self._body(admin.id, a.id, "Admin notified."),
            headers=_service_token_header(),
        )
        assert r.status_code == 200

    # ── success flow ─────────────────────────────────────────────────────────

    def test_success_gives_sent_row_with_sent_at(self, client, db_session, monkeypatch):
        monkeypatch.setattr(settings, "notify_provider", "log")
        dept = _make_dept(db_session)
        citizen = _make_user(db_session, "citizen", f"cit_{_uid()}@p14.test")
        a = _make_app(db_session, citizen.id, dept.id)
        r = client.post(
            "/internal/notifications/send",
            json=self._body(citizen.id, a.id, "All good!"),
            headers=_service_token_header(),
        )
        assert r.status_code == 200
        nid = r.json()["notification_id"]
        db_session.expire_all()
        notif = db_session.get(Notification, nid)
        assert notif is not None
        assert notif.status == "sent"
        assert notif.sent_at is not None

    # ── provider failure ─────────────────────────────────────────────────────

    def test_provider_failure_gives_failed_row_and_500(self, client, db_session, monkeypatch):
        monkeypatch.setattr(settings, "notify_provider", "smtp")
        monkeypatch.setattr(settings, "smtp_host", "unreachable.example.invalid")
        monkeypatch.setattr(settings, "smtp_from", "no-reply@example.com")
        monkeypatch.setattr(settings, "smtp_port", 587)
        monkeypatch.setattr(settings, "smtp_starttls", False)
        monkeypatch.setattr(settings, "smtp_username", "")

        with patch("smtplib.SMTP", side_effect=OSError("unreachable")):
            dept = _make_dept(db_session)
            citizen = _make_user(db_session, "citizen", f"cit_{_uid()}@p14.test")
            a = _make_app(db_session, citizen.id, dept.id)
            r = client.post(
                "/internal/notifications/send",
                json=self._body(citizen.id, a.id, "Test failure."),
                headers=_service_token_header(),
            )

        assert r.status_code == 500
        # The row must still be in DB with status=failed
        db_session.expire_all()
        notifs = (
            db_session.query(Notification)
            .filter(
                Notification.application_id == a.id,
                Notification.recipient_id == citizen.id,
            )
            .all()
        )
        assert any(n.status == "failed" for n in notifs)


# ══════════════════════════════════════════════════════════════════════════════
# STEP 4: GET /notifications
# ══════════════════════════════════════════════════════════════════════════════

class TestNotificationListEndpoint:
    def test_401_without_token(self, client):
        r = client.get("/notifications")
        assert r.status_code == 401

    def test_returns_only_callers_rows(self, client, db_session):
        dept = _make_dept(db_session)
        citizen1 = _make_user(db_session, "citizen", f"cit_{_uid()}@p14.test")
        citizen2 = _make_user(db_session, "citizen", f"cit_{_uid()}@p14.test")
        a = _make_app(db_session, citizen1.id, dept.id)
        _make_notification(db_session, a.id, citizen1.id, "for citizen1")
        _make_notification(db_session, a.id, citizen2.id, "for citizen2")
        db_session.flush()

        r = client.get("/notifications", headers=_token(citizen1))
        assert r.status_code == 200
        items = r.json()
        messages = [i["message"] for i in items]
        assert "for citizen1" in messages
        assert "for citizen2" not in messages

    def test_other_users_rows_never_visible(self, client, db_session):
        dept = _make_dept(db_session)
        citizen1 = _make_user(db_session, "citizen", f"cit_{_uid()}@p14.test")
        citizen2 = _make_user(db_session, "citizen", f"cit_{_uid()}@p14.test")
        a = _make_app(db_session, citizen1.id, dept.id)
        _make_notification(db_session, a.id, citizen1.id, "c1 msg")
        db_session.flush()

        r = client.get("/notifications", headers=_token(citizen2))
        assert r.status_code == 200
        assert r.json() == []

    def test_newest_first(self, client, db_session):
        dept = _make_dept(db_session)
        citizen = _make_user(db_session, "citizen", f"cit_{_uid()}@p14.test")
        a = _make_app(db_session, citizen.id, dept.id)
        n1 = _make_notification(db_session, a.id, citizen.id, "first")
        n2 = _make_notification(db_session, a.id, citizen.id, "second")
        n3 = _make_notification(db_session, a.id, citizen.id, "third")
        db_session.flush()

        r = client.get("/notifications", headers=_token(citizen))
        assert r.status_code == 200
        ids = [i["id"] for i in r.json()]
        # newest first: n3.id > n2.id > n1.id
        assert ids.index(n3.id) < ids.index(n2.id) < ids.index(n1.id)

    def test_limit_works(self, client, db_session):
        dept = _make_dept(db_session)
        citizen = _make_user(db_session, "citizen", f"cit_{_uid()}@p14.test")
        a = _make_app(db_session, citizen.id, dept.id)
        for i in range(5):
            _make_notification(db_session, a.id, citizen.id, f"msg {i}")
        db_session.flush()

        r = client.get("/notifications?limit=3", headers=_token(citizen))
        assert r.status_code == 200
        assert len(r.json()) == 3

    def test_limit_out_of_range_422(self, client, db_session):
        dept = _make_dept(db_session)
        citizen = _make_user(db_session, "citizen", f"cit_{_uid()}@p14.test")
        db_session.flush()
        r = client.get("/notifications?limit=0", headers=_token(citizen))
        assert r.status_code == 422
        r2 = client.get("/notifications?limit=101", headers=_token(citizen))
        assert r2.status_code == 422


# ══════════════════════════════════════════════════════════════════════════════
# STEP 5: Event emitter tests
# ══════════════════════════════════════════════════════════════════════════════

class _WebhookServer(http.server.BaseHTTPRequestHandler):
    """Tiny HTTP server that captures POST bodies into a queue."""

    received: "queue.Queue[dict]" = queue.Queue()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        token = self.headers.get("X-Webhook-Token", "")
        try:
            payload = json.loads(body)
        except Exception:
            payload = {}
        self.__class__.received.put({"payload": payload, "token": token})
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass  # suppress output


def _start_webhook_server() -> tuple[http.server.HTTPServer, int]:
    """Start the server on an ephemeral port and return (server, port)."""
    server = http.server.HTTPServer(("127.0.0.1", 0), _WebhookServer)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, port


class TestEventEmitter:
    def test_status_changed_event_fired_after_transition(self, db_session, monkeypatch):
        """change_status fires one 'status_changed' event with right payload + token."""
        _WebhookServer.received = queue.Queue()
        server, port = _start_webhook_server()
        url = f"http://127.0.0.1:{port}"
        token = "test-webhook-token"
        monkeypatch.setattr(settings, "n8n_webhook_url", url)
        monkeypatch.setattr(settings, "n8n_webhook_token", token)

        from app.services.application_service import change_status
        from app.services.system_actor import get_system_user_id

        dept = _make_dept(db_session)
        citizen = _make_user(db_session, "citizen", f"cit_{_uid()}@p14.test")
        a = _make_app(db_session, citizen.id, dept.id, status=ApplicationStatus.VALIDATED)
        db_session.commit()
        system_id = get_system_user_id(db_session)

        change_status(
            db_session,
            application_id=a.id,
            new_status="ASSIGNED",
            actor="system",
            changed_by=system_id,
        )

        # Wait for the daemon thread to fire
        try:
            received = _WebhookServer.received.get(timeout=3)
        except queue.Empty:
            pytest.fail("Webhook not received within timeout")
        finally:
            server.shutdown()

        assert received["token"] == token
        p = received["payload"]
        assert p["event_type"] == "status_changed"
        assert p["application_id"] == a.id
        assert p["new_status"] == "ASSIGNED"
        assert p["citizen_id"] == citizen.id

    def test_created_event_fired_on_create(self, db_session, monkeypatch):
        """create_application fires a 'created' event."""
        _WebhookServer.received = queue.Queue()
        server, port = _start_webhook_server()
        url = f"http://127.0.0.1:{port}"
        monkeypatch.setattr(settings, "n8n_webhook_url", url)
        monkeypatch.setattr(settings, "n8n_webhook_token", "tok")

        from app.services.application_service import create_application

        dept = _make_dept(db_session)
        citizen = _make_user(db_session, "citizen", f"cit_{_uid()}@p14.test")
        db_session.commit()

        create_application(
            db_session,
            citizen_id=citizen.id,
            service_type="civic_services",
            department_id=dept.id,
        )

        try:
            received = _WebhookServer.received.get(timeout=3)
        except queue.Empty:
            pytest.fail("Created webhook not received within timeout")
        finally:
            server.shutdown()

        assert received["payload"]["event_type"] == "created"

    def test_nothing_sent_without_url(self, db_session, monkeypatch):
        """No webhook fired when N8N_WEBHOOK_URL is empty."""
        _WebhookServer.received = queue.Queue()
        server, port = _start_webhook_server()
        monkeypatch.setattr(settings, "n8n_webhook_url", "")
        monkeypatch.setattr(settings, "n8n_webhook_token", "tok")

        from app.services.application_service import change_status
        from app.services.system_actor import get_system_user_id

        dept = _make_dept(db_session)
        citizen = _make_user(db_session, "citizen", f"cit_{_uid()}@p14.test")
        a = _make_app(db_session, citizen.id, dept.id, status=ApplicationStatus.VALIDATED)
        db_session.commit()
        system_id = get_system_user_id(db_session)

        change_status(
            db_session,
            application_id=a.id,
            new_status="ASSIGNED",
            actor="system",
            changed_by=system_id,
        )
        time.sleep(0.2)  # give any accidental thread time to fire

        server.shutdown()
        assert _WebhookServer.received.empty()

    def test_nothing_sent_without_token(self, db_session, monkeypatch):
        """No webhook fired when N8N_WEBHOOK_TOKEN is empty."""
        _WebhookServer.received = queue.Queue()
        server, port = _start_webhook_server()
        monkeypatch.setattr(settings, "n8n_webhook_url", f"http://127.0.0.1:{port}")
        monkeypatch.setattr(settings, "n8n_webhook_token", "")

        from app.services.application_service import change_status
        from app.services.system_actor import get_system_user_id

        dept = _make_dept(db_session)
        citizen = _make_user(db_session, "citizen", f"cit_{_uid()}@p14.test")
        a = _make_app(db_session, citizen.id, dept.id, status=ApplicationStatus.VALIDATED)
        db_session.commit()
        system_id = get_system_user_id(db_session)

        change_status(
            db_session,
            application_id=a.id,
            new_status="ASSIGNED",
            actor="system",
            changed_by=system_id,
        )
        time.sleep(0.2)

        server.shutdown()
        assert _WebhookServer.received.empty()

    def test_hanging_endpoint_does_not_delay_transition(self, db_session, monkeypatch):
        """A slow webhook must not noticeably delay a transition (2s timeout)."""
        # Use a socket that accepts but never responds
        slow_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        slow_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        slow_sock.bind(("127.0.0.1", 0))
        slow_sock.listen(5)
        port = slow_sock.getsockname()[1]
        # Accept connections but never reply (simulates a hang)

        def _accept_loop():
            while True:
                try:
                    conn, _ = slow_sock.accept()
                    # deliberately do not send anything
                except OSError:
                    break

        threading.Thread(target=_accept_loop, daemon=True).start()

        monkeypatch.setattr(settings, "n8n_webhook_url", f"http://127.0.0.1:{port}")
        monkeypatch.setattr(settings, "n8n_webhook_token", "tok")

        from app.services.application_service import change_status
        from app.services.system_actor import get_system_user_id

        dept = _make_dept(db_session)
        citizen = _make_user(db_session, "citizen", f"cit_{_uid()}@p14.test")
        a = _make_app(db_session, citizen.id, dept.id, status=ApplicationStatus.VALIDATED)
        db_session.commit()
        system_id = get_system_user_id(db_session)

        start = time.perf_counter()
        result = change_status(
            db_session,
            application_id=a.id,
            new_status="ASSIGNED",
            actor="system",
            changed_by=system_id,
        )
        elapsed = time.perf_counter() - start

        slow_sock.close()
        assert result is not None
        # Transition must return well inside the 2s webhook timeout (daemon thread)
        assert elapsed < 1.0, f"Transition took {elapsed:.2f}s — too slow"

    def test_refused_endpoint_does_not_break_transition(self, db_session, monkeypatch):
        """A refused connection must not break or delay a transition."""
        monkeypatch.setattr(settings, "n8n_webhook_url", "http://127.0.0.1:19999")
        monkeypatch.setattr(settings, "n8n_webhook_token", "tok")

        from app.services.application_service import change_status
        from app.services.system_actor import get_system_user_id

        dept = _make_dept(db_session)
        citizen = _make_user(db_session, "citizen", f"cit_{_uid()}@p14.test")
        a = _make_app(db_session, citizen.id, dept.id, status=ApplicationStatus.VALIDATED)
        db_session.commit()
        system_id = get_system_user_id(db_session)

        start = time.perf_counter()
        result = change_status(
            db_session,
            application_id=a.id,
            new_status="ASSIGNED",
            actor="system",
            changed_by=system_id,
        )
        elapsed = time.perf_counter() - start
        assert result is not None
        assert elapsed < 1.0

    def test_rolled_back_change_fires_nothing(self, db_session, monkeypatch):
        """An invalid transition (rolled back) must not fire any event."""
        _WebhookServer.received = queue.Queue()
        server, port = _start_webhook_server()
        monkeypatch.setattr(settings, "n8n_webhook_url", f"http://127.0.0.1:{port}")
        monkeypatch.setattr(settings, "n8n_webhook_token", "tok")

        from app.services.application_service import change_status
        from app.services.state_machine import InvalidTransitionError
        from app.services.system_actor import get_system_user_id

        dept = _make_dept(db_session)
        citizen = _make_user(db_session, "citizen", f"cit_{_uid()}@p14.test")
        # DOCUMENT_PENDING → APPROVED is illegal
        a = _make_app(db_session, citizen.id, dept.id, status=ApplicationStatus.DOCUMENT_PENDING)
        db_session.commit()
        system_id = get_system_user_id(db_session)

        with pytest.raises(InvalidTransitionError):
            change_status(
                db_session,
                application_id=a.id,
                new_status="APPROVED",
                actor="officer",
                changed_by=system_id,
            )

        time.sleep(0.2)
        server.shutdown()
        assert _WebhookServer.received.empty()
