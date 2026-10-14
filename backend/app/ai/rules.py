"""Rule-based document validation configuration.

DEMO CONFIG — edit this file to match the service types and required
documents for your deployment.  All logic is rule-based (no ML model).
Never add accuracy, precision, or recall numbers to this file.
"""

from __future__ import annotations

import re


# ── Required document types per service_type ─────────────────────────────────
# Keys must match the service_type values in database/seed/seed.py exactly
# (comparison is case-insensitive and trimmed at runtime).
# These are the two seeded service_types:
#   "civic_services"  (Municipal Corporation)
#   "land_records"    (Revenue Department)

REQUIRED_DOCUMENTS: dict[str, list[str]] = {
    "civic_services": ["ID_PROOF", "ADDRESS_PROOF"],
    "land_records":   ["ID_PROOF", "ADDRESS_PROOF"],
}

# Fallback for any service_type not in the dict above.
DEFAULT_REQUIRED: list[str] = ["ID_PROOF"]


# ── Per-doc-type validation checks ───────────────────────────────────────────
# Each entry is a list of check names (strings).  The validator.py module
# maps each name to the actual check function.
#
# Available checks:
#   "min_text"        — text length >= OCR_MIN_TEXT_CHARS (rule-based)
#   "applicant_name"  — every name token (2+ chars) from full_name appears
#                       as a whole word in normalised text (rule-based)
#   "id_number"       — 12-digit number (with optional spaces) OR PAN pattern
#                       [A-Z]{5}[0-9]{4}[A-Z]{1} (rule-based)

DOCUMENT_RULES: dict[str, list[str]] = {
    "ID_PROOF":      ["min_text", "applicant_name", "id_number"],
    "ADDRESS_PROOF": ["min_text", "applicant_name"],
    # Fallback for unknown doc types: min_text only
    "__default__":   ["min_text"],
}

# ── Compiled patterns (rule-based) ───────────────────────────────────────────
# 12-digit number with optional single spaces between groups of 4:
#   e.g.  123456789012   1234 5678 9012
# OR PAN-style:  ABCDE1234F
_ID_NUMBER_RE = re.compile(
    r"""
    \b(?:
        \d{4}[ ]?\d{4}[ ]?\d{4}   # 12 digits, optional spaces
      | [A-Z]{5}\d{4}[A-Z]        # PAN pattern
    )\b
    """,
    re.VERBOSE,
)


def get_required_doc_types(service_type: str) -> list[str]:
    """Return the required doc types for *service_type* (case-insensitive)."""
    key = (service_type or "").strip().lower()
    for k, v in REQUIRED_DOCUMENTS.items():
        if k.lower() == key:
            return list(v)
    return list(DEFAULT_REQUIRED)


def get_checks_for_doc_type(doc_type: str) -> list[str]:
    """Return the check list for *doc_type*, falling back to __default__."""
    normalised = (doc_type or "").strip().upper()
    return list(DOCUMENT_RULES.get(normalised, DOCUMENT_RULES["__default__"]))
