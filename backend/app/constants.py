"""Application-wide constants.

The database column ``applications.priority`` is an ``Integer``.
This enum provides the canonical label ↔ integer mapping.
"""

from __future__ import annotations

import enum


class Priority(int, enum.Enum):
    LOW = 0
    MEDIUM = 1
    HIGH = 2

    @classmethod
    def from_label(cls, label: str) -> "Priority":
        """Parse a label (case-insensitive).  Raises ValueError for unknowns."""
        try:
            return cls[label.upper()]
        except KeyError:
            valid = ", ".join(p.name for p in cls)
            raise ValueError(f"Invalid priority '{label}'. Valid values: {valid}.")

    @property
    def label(self) -> str:
        return self.name


# ── Document validation statuses ─────────────────────────────────────────────
# Stored as plain strings in the documents.validation_status column.

VALIDATION_STATUS_PENDING = "PENDING"
VALIDATION_STATUS_VALID   = "VALID"
VALIDATION_STATUS_INVALID = "INVALID"
VALIDATION_STATUS_ERROR   = "ERROR"


# ── Audit action names ────────────────────────────────────────────────────────
# Use these constants everywhere — never hard-code strings.

class AuditAction:
    APPLICATION_CREATE           = "application.create"
    APPLICATION_STATUS_CHANGE    = "application.status_change"
    APPLICATION_REQUEST_DOCUMENTS = "application.request_documents"
    ASSIGNMENT_CREATE            = "assignment.create"
    DOCUMENT_UPLOAD              = "document.upload"
    DOCUMENT_VIEW                = "document.view"
    DOCUMENT_ANALYSIS_VIEW       = "document.analysis_view"
    ESCALATION_CREATE            = "escalation.create"
    ESCALATION_RESOLVE           = "escalation.resolve"
    NOTIFICATION_SEND            = "notification.send"
    PRIORITY_REEVALUATE          = "priority.reevaluate"
    AUTH_REGISTER                = "auth.register"
    AUTH_LOGIN_SUCCESS           = "auth.login_success"
    AUTH_LOGIN_FAILURE           = "auth.login_failure"


class AuditEntity:
    APPLICATION  = "application"
    DOCUMENT     = "document"
    ESCALATION   = "escalation"
    NOTIFICATION = "notification"
    USER         = "user"
