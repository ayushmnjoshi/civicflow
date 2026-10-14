"""Rule-based priority decisions for new applications.

This is a deterministic rules engine, not a machine-learning model.  The
configuration values below are intentionally simple for the system owner to edit.
"""

from __future__ import annotations

from app.constants import Priority


# Owner-editable rule configuration.  Values match database/seed/seed.py.
WAITING_HIGH_HOURS = 72
STANDARD_SERVICE_TYPES = frozenset({"civic_services", "land_records"})


def decide_priority(
    *,
    urgent: bool,
    waiting_hours: float,
    department_service_type: str | None,
) -> tuple[Priority, str]:
    """Return the priority and stable reason code for the ordered rule tree."""
    if urgent:
        return Priority.HIGH, "urgent_flag"
    if waiting_hours >= WAITING_HIGH_HOURS:
        return Priority.HIGH, "waiting_time"
    if (department_service_type or "").strip().lower() in STANDARD_SERVICE_TYPES:
        return Priority.MEDIUM, "standard_category"
    return Priority.LOW, "other_category"
