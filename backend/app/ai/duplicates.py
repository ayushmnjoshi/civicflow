"""Pure, exact duplicate-detection helpers.

This is a deterministic rules engine, not a machine-learning model; it performs
no fuzzy matching.
"""

from __future__ import annotations

import re


def normalize_name(value: str | None) -> frozenset[str]:
    """Return case-normalized name tokens with punctuation and order ignored."""
    cleaned = re.sub(r"[\W_]+", " ", (value or "").lower())
    return frozenset(cleaned.split())


def normalize_phone(value: str | None) -> str | None:
    """Return the last ten phone digits, or None when no digits were supplied."""
    digits = "".join(char for char in (value or "") if char.isdigit())
    return digits[-10:] if digits else None


def normalize_service(value: str | None) -> str:
    """Normalize a service label for exact comparison."""
    return " ".join((value or "").lower().split())


def _status_value(status: object) -> str:
    return str(getattr(status, "value", status))


def is_duplicate(
    application: object,
    citizen: object,
    candidate: object,
    candidate_citizen: object,
) -> bool:
    """Return whether one earlier candidate exactly matches an application.

    Objects need only expose the application and user attributes used below,
    keeping this function independent from SQLAlchemy and straightforward to test.
    """
    if getattr(candidate, "id") >= getattr(application, "id"):
        return False
    if getattr(candidate, "department_id") != getattr(application, "department_id"):
        return False
    if _status_value(getattr(candidate, "status")) == "REJECTED":
        return False
    if normalize_service(getattr(candidate, "service_type")) != normalize_service(
        getattr(application, "service_type")
    ):
        return False
    if getattr(candidate, "citizen_id") == getattr(application, "citizen_id"):
        return True

    candidate_name = normalize_name(getattr(candidate_citizen, "full_name", None))
    citizen_name = normalize_name(getattr(citizen, "full_name", None))
    candidate_phone = normalize_phone(getattr(candidate_citizen, "phone", None))
    citizen_phone = normalize_phone(getattr(citizen, "phone", None))
    return bool(candidate_name) and candidate_name == citizen_name and bool(
        candidate_phone
    ) and candidate_phone == citizen_phone
