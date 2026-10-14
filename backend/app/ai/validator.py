"""Rule-based document and application validator.

All functions are pure (no DB, no I/O) so they are easy to unit-test.
Logic is rule-based — never reports accuracy/precision/recall numbers.
Never logs OCR text or ID numbers.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from app.ai.rules import (
    _ID_NUMBER_RE,
    get_checks_for_doc_type,
    get_required_doc_types,
)


# ── Name normalisation ────────────────────────────────────────────────────────

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)


def _normalise(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    text = unicodedata.normalize("NFC", text).lower()
    text = _PUNCT_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def _name_tokens(full_name: str) -> list[str]:
    """Return tokens from *full_name* that are at least 2 characters long."""
    return [t for t in _normalise(full_name).split() if len(t) >= 2]


# ── Individual check functions ────────────────────────────────────────────────

def _check_min_text(text: str, **_: Any) -> bool:
    from app.config import settings
    return len(text.strip()) >= settings.ocr_min_text_chars


def _check_applicant_name(text: str, applicant_name: str, **_: Any) -> bool:
    """Every name token (2+ chars) must appear as a whole word in the text."""
    tokens = _name_tokens(applicant_name)
    if not tokens:
        return False
    normalised_text = _normalise(text)
    for token in tokens:
        # whole-word match using \b
        pattern = r"\b" + re.escape(token) + r"\b"
        if not re.search(pattern, normalised_text):
            return False
    return True


def _check_id_number(text: str, **_: Any) -> bool:
    """Rule-based: look for a 12-digit number or PAN pattern in the text."""
    return bool(_ID_NUMBER_RE.search(text))


_CHECK_FNS = {
    "min_text":       _check_min_text,
    "applicant_name": _check_applicant_name,
    "id_number":      _check_id_number,
}


# ── Document validator ────────────────────────────────────────────────────────

def validate_document(
    doc_type: str,
    text: str,
    applicant_name: str,
) -> dict[str, Any]:
    """Run all configured checks for *doc_type* against *text*.

    Returns::

        {
            "result": "VALID" | "INVALID",
            "checks": [{"name": str, "passed": bool}, ...],
            "reasons": [str, ...],         # reason codes only, no doc content
            "extracted_fields": {
                "name_found": bool,
                "id_number_masked": "XXXXXXXX1234" | None,
            },
        }

    Rule-based — never reports accuracy or precision numbers.
    Never logs or stores raw OCR text or ID values.
    """
    checks_config = get_checks_for_doc_type(doc_type)
    ctx = {"text": text, "applicant_name": applicant_name}

    check_results: list[dict[str, Any]] = []
    reasons: list[str] = []

    for check_name in checks_config:
        fn = _CHECK_FNS.get(check_name)
        if fn is None:
            continue
        passed = fn(**ctx)
        check_results.append({"name": check_name, "passed": passed})
        if not passed:
            reasons.append(f"check_failed:{check_name}")

    result = "VALID" if all(c["passed"] for c in check_results) else "INVALID"

    # Extracted fields — mask sensitive values
    id_match = _ID_NUMBER_RE.search(text)
    id_masked: str | None = None
    if id_match:
        raw = re.sub(r"\s", "", id_match.group())  # remove spaces
        id_masked = "X" * (len(raw) - 4) + raw[-4:]

    extracted_fields: dict[str, Any] = {
        "name_found": _check_applicant_name(text, applicant_name),
        "id_number_masked": id_masked,
    }

    return {
        "result": result,
        "checks": check_results,
        "reasons": reasons,
        "extracted_fields": extracted_fields,
    }


# ── Application-level validator ───────────────────────────────────────────────

def validate_application(
    required_doc_types: list[str],
    documents: list[dict[str, Any]],
) -> dict[str, Any]:
    """Decide VALIDATED or ADDITIONAL_DOCUMENT_REQUIRED for an application.

    *documents* is a list of dicts with keys: id, doc_type, validation_status.
    Uses the LATEST document (highest id) for each required doc_type.

    Returns::

        {
            "result": "VALIDATED" | "ADDITIONAL_DOCUMENT_REQUIRED",
            "missing_docs": [str, ...],
            "invalid_docs": [str, ...],
        }
    """
    # Group by doc_type, pick the latest (highest id)
    latest: dict[str, dict[str, Any]] = {}
    for doc in documents:
        dt = (doc.get("doc_type") or "").strip().upper()
        if dt not in latest or doc["id"] > latest[dt]["id"]:
            latest[dt] = doc

    missing: list[str] = []
    invalid: list[str] = []

    for req in required_doc_types:
        req_upper = req.strip().upper()
        if req_upper not in latest:
            missing.append(req_upper)
        elif latest[req_upper].get("validation_status") != "VALID":
            invalid.append(req_upper)

    if missing or invalid:
        return {
            "result": "ADDITIONAL_DOCUMENT_REQUIRED",
            "missing_docs": missing,
            "invalid_docs": invalid,
        }

    return {
        "result": "VALIDATED",
        "missing_docs": [],
        "invalid_docs": [],
    }
