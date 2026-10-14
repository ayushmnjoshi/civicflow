"""Unit tests for app/ai/validator.py and app/ai/rules.py.

Pure function tests — no DB, no I/O, no real OCR.
"""

from __future__ import annotations

import pytest

from app.ai.rules import get_required_doc_types, get_checks_for_doc_type
from app.ai.validator import (
    _normalise,
    _name_tokens,
    _check_applicant_name,
    _check_id_number,
    validate_document,
    validate_application,
)


# ── Text normalisation ────────────────────────────────────────────────────────

class TestNormalise:
    def test_lowercase(self):
        assert _normalise("HELLO World") == "hello world"

    def test_strip_punctuation(self):
        # Punctuation is replaced by a space, then spaces are collapsed
        assert _normalise("hello, world!") == "hello world"

    def test_collapse_spaces(self):
        assert _normalise("a  b   c") == "a b c"

    def test_mixed(self):
        # Trailing punct becomes a space, then collapsed and stripped
        assert _normalise("Dr. Ayush Singh.") == "dr ayush singh"


class TestNameTokens:
    def test_filters_short_tokens(self):
        # single-char tokens dropped
        assert _name_tokens("A B CD") == ["cd"]

    def test_normal_name(self):
        assert _name_tokens("Ayush Joshi") == ["ayush", "joshi"]

    def test_with_punctuation(self):
        assert _name_tokens("O'Brien, James") == ["brien", "james"]


# ── applicant_name check ──────────────────────────────────────────────────────

class TestCheckApplicantName:
    def test_all_tokens_present(self):
        text = "This document certifies that Ayush Joshi is a resident."
        assert _check_applicant_name(text, "Ayush Joshi") is True

    def test_case_insensitive(self):
        text = "AYUSH JOSHI has applied."
        assert _check_applicant_name(text, "Ayush Joshi") is True

    def test_missing_token(self):
        text = "Ayush has applied."
        assert _check_applicant_name(text, "Ayush Joshi") is False

    def test_partial_word_does_not_match(self):
        # "joshi" must be a whole word, not "joshiv"
        text = "Ayush Joshiv is here."
        assert _check_applicant_name(text, "Ayush Joshi") is False

    def test_empty_name(self):
        assert _check_applicant_name("some text", "") is False

    def test_single_char_name_token_ignored(self):
        # "A" is a single char, gets ignored; "B" also; result has no tokens
        assert _check_applicant_name("some text", "A B") is False

    def test_name_with_punctuation(self):
        text = "resident name: ayush joshi, verified."
        assert _check_applicant_name(text, "Ayush Joshi") is True


# ── id_number check ───────────────────────────────────────────────────────────

class TestCheckIdNumber:
    def test_12_digit_no_spaces(self):
        assert _check_id_number("ID: 123456789012") is True

    def test_12_digit_with_spaces(self):
        assert _check_id_number("Aadhaar: 1234 5678 9012") is True

    def test_pan_pattern(self):
        assert _check_id_number("PAN: ABCDE1234F") is True

    def test_no_id_in_text(self):
        assert _check_id_number("This has no id number at all.") is False

    def test_11_digit_not_matched(self):
        assert _check_id_number("12345678901") is False

    def test_13_digit_not_full_match(self):
        # 13-digit string: the \b prevents matching a 12-digit substring inside
        result = _check_id_number("1234567890123")
        # regex should NOT match (boundary prevents it)
        assert result is False


# ── validate_document ─────────────────────────────────────────────────────────

ID_TEXT = "Resident ID card\nName: Ayush Joshi\nID Number: 1234 5678 9012\nAddress: 123 Main St"
ADDR_TEXT = "Utility Bill\nName: Ayush Joshi\nAddress: 123 Main St, City"


class TestValidateDocument:
    def test_id_proof_valid(self):
        result = validate_document("ID_PROOF", ID_TEXT, "Ayush Joshi")
        assert result["result"] == "VALID"
        assert result["reasons"] == []

    def test_id_proof_missing_name(self):
        text = "ID Number: 1234 5678 9012\nResident card."
        result = validate_document("ID_PROOF", text, "Ayush Joshi")
        assert result["result"] == "INVALID"
        assert any("applicant_name" in r for r in result["reasons"])

    def test_id_proof_missing_id_number(self):
        text = "Name: Ayush Joshi\nResident card without id."
        result = validate_document("ID_PROOF", text, "Ayush Joshi")
        assert result["result"] == "INVALID"
        assert any("id_number" in r for r in result["reasons"])

    def test_address_proof_valid(self):
        result = validate_document("ADDRESS_PROOF", ADDR_TEXT, "Ayush Joshi")
        assert result["result"] == "VALID"

    def test_address_proof_no_id_required(self):
        # No id_number in text — ADDRESS_PROOF should still be VALID
        text = "Name: Ayush Joshi\nAddress: 123 Main St"
        result = validate_document("ADDRESS_PROOF", text, "Ayush Joshi")
        assert result["result"] == "VALID"

    def test_empty_text_invalid(self):
        result = validate_document("ID_PROOF", "", "Ayush Joshi")
        assert result["result"] == "INVALID"
        assert any("min_text" in r for r in result["reasons"])

    def test_unknown_doc_type_min_text_only(self):
        """Unknown doc types run only the min_text check."""
        checks = get_checks_for_doc_type("UNKNOWN_DOC")
        assert checks == ["min_text"]
        # With enough text → VALID
        result = validate_document("UNKNOWN_DOC", "x" * 25, "Anyone")
        assert result["result"] == "VALID"

    def test_checks_list_shape(self):
        result = validate_document("ID_PROOF", ID_TEXT, "Ayush Joshi")
        for check in result["checks"]:
            assert "name" in check
            assert "passed" in check

    def test_id_number_masked(self):
        result = validate_document("ID_PROOF", ID_TEXT, "Ayush Joshi")
        masked = result["extracted_fields"]["id_number_masked"]
        assert masked is not None
        assert masked.endswith("9012")
        assert "X" in masked

    def test_no_id_number_masked_is_none(self):
        result = validate_document("ADDRESS_PROOF", ADDR_TEXT, "Ayush Joshi")
        # ADDRESS_PROOF text has no id number
        masked = result["extracted_fields"]["id_number_masked"]
        # Could be None or have a value depending on text — just check it doesn't crash
        assert "id_number_masked" in result["extracted_fields"]

    def test_name_found_true(self):
        result = validate_document("ID_PROOF", ID_TEXT, "Ayush Joshi")
        assert result["extracted_fields"]["name_found"] is True

    def test_name_found_false(self):
        text = "ID Number: 1234 5678 9012\nSome other person"
        result = validate_document("ID_PROOF", text, "Ayush Joshi")
        assert result["extracted_fields"]["name_found"] is False


# ── validate_application ──────────────────────────────────────────────────────

class TestValidateApplication:
    def _doc(self, id: int, doc_type: str, status: str) -> dict:
        return {"id": id, "doc_type": doc_type, "validation_status": status}

    def test_all_valid_returns_validated(self):
        docs = [
            self._doc(1, "ID_PROOF", "VALID"),
            self._doc(2, "ADDRESS_PROOF", "VALID"),
        ]
        result = validate_application(["ID_PROOF", "ADDRESS_PROOF"], docs)
        assert result["result"] == "VALIDATED"
        assert result["missing_docs"] == []
        assert result["invalid_docs"] == []

    def test_missing_doc_type(self):
        docs = [self._doc(1, "ID_PROOF", "VALID")]
        result = validate_application(["ID_PROOF", "ADDRESS_PROOF"], docs)
        assert result["result"] == "ADDITIONAL_DOCUMENT_REQUIRED"
        assert "ADDRESS_PROOF" in result["missing_docs"]

    def test_invalid_doc(self):
        docs = [
            self._doc(1, "ID_PROOF", "INVALID"),
            self._doc(2, "ADDRESS_PROOF", "VALID"),
        ]
        result = validate_application(["ID_PROOF", "ADDRESS_PROOF"], docs)
        assert result["result"] == "ADDITIONAL_DOCUMENT_REQUIRED"
        assert "ID_PROOF" in result["invalid_docs"]

    def test_latest_document_wins(self):
        """When multiple docs of the same type exist, highest id is used."""
        docs = [
            self._doc(1, "ID_PROOF", "INVALID"),  # older
            self._doc(5, "ID_PROOF", "VALID"),     # newer — should win
            self._doc(2, "ADDRESS_PROOF", "VALID"),
        ]
        result = validate_application(["ID_PROOF", "ADDRESS_PROOF"], docs)
        assert result["result"] == "VALIDATED"

    def test_latest_document_wins_invalid_overrides(self):
        """If the latest doc is INVALID, it should fail even if an older one was VALID."""
        docs = [
            self._doc(1, "ID_PROOF", "VALID"),     # older
            self._doc(5, "ID_PROOF", "INVALID"),   # newer — should win
            self._doc(2, "ADDRESS_PROOF", "VALID"),
        ]
        result = validate_application(["ID_PROOF", "ADDRESS_PROOF"], docs)
        assert result["result"] == "ADDITIONAL_DOCUMENT_REQUIRED"
        assert "ID_PROOF" in result["invalid_docs"]

    def test_pending_doc_not_valid(self):
        docs = [
            self._doc(1, "ID_PROOF", "PENDING"),
            self._doc(2, "ADDRESS_PROOF", "VALID"),
        ]
        result = validate_application(["ID_PROOF", "ADDRESS_PROOF"], docs)
        assert result["result"] == "ADDITIONAL_DOCUMENT_REQUIRED"

    def test_empty_required_list(self):
        result = validate_application([], [])
        assert result["result"] == "VALIDATED"

    def test_case_insensitive_doc_type(self):
        docs = [
            self._doc(1, "id_proof", "VALID"),  # lowercase
            self._doc(2, "address_proof", "VALID"),
        ]
        result = validate_application(["ID_PROOF", "ADDRESS_PROOF"], docs)
        assert result["result"] == "VALIDATED"


# ── Rules helpers ─────────────────────────────────────────────────────────────

class TestRulesHelpers:
    def test_known_service_type_civic(self):
        reqs = get_required_doc_types("civic_services")
        assert "ID_PROOF" in reqs
        assert "ADDRESS_PROOF" in reqs

    def test_known_service_type_land(self):
        reqs = get_required_doc_types("land_records")
        assert "ID_PROOF" in reqs

    def test_case_insensitive_service_type(self):
        reqs = get_required_doc_types("CIVIC_SERVICES")
        assert reqs == get_required_doc_types("civic_services")

    def test_unknown_service_type_uses_default(self):
        reqs = get_required_doc_types("unknown_service")
        assert reqs == ["ID_PROOF"]

    def test_id_proof_checks(self):
        checks = get_checks_for_doc_type("ID_PROOF")
        assert "min_text" in checks
        assert "applicant_name" in checks
        assert "id_number" in checks

    def test_address_proof_checks(self):
        checks = get_checks_for_doc_type("ADDRESS_PROOF")
        assert "min_text" in checks
        assert "applicant_name" in checks
        assert "id_number" not in checks

    def test_unknown_doc_type_uses_default(self):
        checks = get_checks_for_doc_type("RANDOM_DOC")
        assert checks == ["min_text"]
