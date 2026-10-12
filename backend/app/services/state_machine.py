"""Pure-Python state machine for application status transitions.

No database access. Import and call freely from any layer.
"""

from __future__ import annotations

# ── Transition table ──────────────────────────────────────────────────────────
# (from_status, to_status) → actor who may trigger it: "citizen" | "officer" | "system"

TRANSITIONS: dict[tuple[str, str], str] = {
    ("SUBMITTED", "DOCUMENT_PENDING"):                      "system",
    ("DOCUMENT_PENDING", "VALIDATING"):                     "system",
    ("VALIDATING", "VALIDATED"):                            "system",
    ("VALIDATING", "ADDITIONAL_DOCUMENT_REQUIRED"):         "system",
    ("VALIDATED", "ASSIGNED"):                              "system",
    ("ASSIGNED", "UNDER_REVIEW"):                           "officer",
    ("UNDER_REVIEW", "ADDITIONAL_DOCUMENT_REQUIRED"):       "officer",   # remarks required
    ("ADDITIONAL_DOCUMENT_REQUIRED", "DOCUMENT_UPLOADED"):  "citizen",
    ("DOCUMENT_UPLOADED", "UNDER_REVIEW"):                  "system",
    ("UNDER_REVIEW", "APPROVED"):                           "officer",
    ("UNDER_REVIEW", "REJECTED"):                           "officer",   # remarks required
}

# Transitions where non-empty remarks are mandatory
REMARKS_REQUIRED: frozenset[tuple[str, str]] = frozenset({
    ("UNDER_REVIEW", "ADDITIONAL_DOCUMENT_REQUIRED"),
    ("UNDER_REVIEW", "REJECTED"),
})

# Terminal statuses — no outgoing transitions
TERMINAL: frozenset[str] = frozenset({"APPROVED", "REJECTED"})


# ── Custom exception ──────────────────────────────────────────────────────────

class InvalidTransitionError(Exception):
    """Raised when a status transition is not permitted."""

    def __init__(self, from_status: str, to_status: str, reason: str = "") -> None:
        self.from_status = from_status
        self.to_status = to_status
        self.reason = reason
        super().__init__(
            reason or f"Transition {from_status} → {to_status} is not allowed."
        )


# ── Public API ────────────────────────────────────────────────────────────────

def can_transition(from_status: str, to_status: str, actor: str) -> bool:
    """Return True if *actor* may move an application from *from_status* to *to_status*."""
    if from_status in TERMINAL:
        return False
    allowed_actor = TRANSITIONS.get((from_status, to_status))
    return allowed_actor == actor


def validate_transition(
    from_status: str,
    to_status: str,
    actor: str,
    remarks: str | None = None,
) -> None:
    """Assert that the transition is legal; raise InvalidTransitionError otherwise.

    Also raises if remarks are required but absent/empty.
    """
    if from_status in TERMINAL:
        raise InvalidTransitionError(
            from_status, to_status,
            f"Status '{from_status}' is terminal — no further transitions are allowed.",
        )

    allowed_actor = TRANSITIONS.get((from_status, to_status))
    if allowed_actor is None:
        raise InvalidTransitionError(
            from_status, to_status,
            f"Transition {from_status} \u2192 {to_status} is not defined.",
        )

    if allowed_actor != actor:
        raise InvalidTransitionError(
            from_status, to_status,
            f"Transition {from_status} \u2192 {to_status} requires actor '{allowed_actor}', "
            f"but '{actor}' was provided.",
        )

    if (from_status, to_status) in REMARKS_REQUIRED and not (remarks or "").strip():
        raise InvalidTransitionError(
            from_status, to_status,
            f"Remarks are required for transition {from_status} \u2192 {to_status}.",
        )
