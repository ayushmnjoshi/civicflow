"""Notification service — provider interface and concrete implementations.

Two providers:
  - LogProvider  : logs only the notification id (no address, no body).
  - SmtpProvider : sends via SMTP; fails closed on missing config.

An unknown NOTIFY_PROVIDER value is treated like a misconfigured SMTP.
"""

from __future__ import annotations

import logging
import smtplib
from abc import ABC, abstractmethod
from email.message import EmailMessage

from app.config import settings

logger = logging.getLogger(__name__)


# ── Provider interface ────────────────────────────────────────────────────────

class AbstractNotificationProvider(ABC):
    """Send a notification and return (status, reason_code).

    Returns:
        ("sent", "")        on success
        ("failed", code)    on any failure (reason_code is a short tag, no PII)
    """

    @abstractmethod
    def send(
        self,
        notification_id: int,
        application_id: int,
        recipient_email: str,
        message: str,
    ) -> tuple[str, str]:
        ...  # pragma: no cover


# ── LogProvider ───────────────────────────────────────────────────────────────

class LogProvider(AbstractNotificationProvider):
    """Does nothing except log the notification id.  No address, no body."""

    def send(
        self,
        notification_id: int,
        application_id: int,
        recipient_email: str,
        message: str,
    ) -> tuple[str, str]:
        logger.info("notification_id=%d provider=log status=sent", notification_id)
        return ("sent", "")


# ── SmtpProvider ──────────────────────────────────────────────────────────────

class SmtpProvider(AbstractNotificationProvider):
    """Send via SMTP.

    Required settings: smtp_host, smtp_from.
    STARTTLS: enabled when settings.smtp_starttls is True.
    Login: performed only when settings.smtp_username is non-empty.
    Timeout: 10 seconds.
    """

    def send(
        self,
        notification_id: int,
        application_id: int,
        recipient_email: str,
        message: str,
    ) -> tuple[str, str]:
        host = settings.smtp_host
        from_addr = settings.smtp_from

        if not host or not from_addr:
            logger.error(
                "notification_id=%d reason_code=smtp_not_configured",
                notification_id,
            )
            return ("failed", "smtp_not_configured")

        # Build the message using EmailMessage so header injection is impossible.
        # The body is set with set_content(), which encodes it as a proper MIME
        # part — CR/LF sequences in *message* cannot create new headers.
        msg = EmailMessage()
        msg["Subject"] = f"CivicFlow: update on application #{application_id}"
        msg["From"] = from_addr
        msg["To"] = recipient_email
        msg.set_content(message)

        try:
            with smtplib.SMTP(host, settings.smtp_port, timeout=10) as smtp:
                if settings.smtp_starttls:
                    smtp.starttls()
                if settings.smtp_username:
                    smtp.login(settings.smtp_username, settings.smtp_password)
                smtp.send_message(msg)
        except Exception as exc:
            logger.error(
                "notification_id=%d reason_code=smtp_send_failed error_class=%s",
                notification_id,
                type(exc).__name__,
            )
            return ("failed", "smtp_send_failed")

        logger.info("notification_id=%d provider=smtp status=sent", notification_id)
        return ("sent", "")


# ── Factory ───────────────────────────────────────────────────────────────────

class _UnknownProvider(AbstractNotificationProvider):
    """Fail-closed stub for an unrecognised NOTIFY_PROVIDER value."""

    def send(
        self,
        notification_id: int,
        application_id: int,
        recipient_email: str,
        message: str,
    ) -> tuple[str, str]:
        logger.error(
            "notification_id=%d reason_code=unknown_provider value=%s",
            notification_id,
            settings.notify_provider,
        )
        return ("failed", "unknown_provider")


def get_provider() -> AbstractNotificationProvider:
    """Return the configured provider, never raises."""
    prov = (settings.notify_provider or "log").lower()
    if prov == "log":
        return LogProvider()
    if prov == "smtp":
        return SmtpProvider()
    return _UnknownProvider()
