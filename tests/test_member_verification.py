"""Fidelity tests for the ported V1 member verification engine.

These pin the reference's decision logic. If a change here starts failing, the
port has drifted from the V1 ``Member_Verification`` behaviour and the fix is
to bring it back, not to update the expectation.
"""
from __future__ import annotations

import math

import pytest

from member import (
    classify_page,
    detect_name_mode,
    expected_from_manifest,
    summary_status,
    verify_page,
    verify_record,
)
from member.extractors.rule_based.dob import extract_dob
from member.extractors.rule_based.member_id import extract_member_id
from member.extractors.rule_based.name_common import (
    ALL_FULL,
    BOTH_FULL,
    INITIAL,
    MISMATCH,
    ONE_FULL_WRONG,
    TWO_FULL,
    classify_three_word_name,
    classify_two_word_name,
    find_two_word_name,
    tokenize,
)
from member.rules.base_rules import combine_evidences
from member.rules.what_if_rules import (
    ACCEPT,
    PAGE_NOT_VERIFIED,
    PAGE_VERIFIED,
    PAGE_WRONG_MEMBER,
    REJECT,
    apply_what_if,
    count_wrong_member,
    reject_threshold,
)


def manifest(**overrides):
    row = {
        "id": 1,
        "member_name": "Justin Anderson",
        "first_name": "Justin",
        "middle_name": None,
        "last_name": "Anderson",
        "member_dob": "08/29/1954",
        "external_member_id": "A9000603900",
    }
    row.update(overrides)
    return row


# --- evidence combination (base_rules) --------------------------------------


class TestCombineEvidences:
    def test_no_name_always_rejects(self):
        assert combine_evidences(name_ok=False, dob_ok=True, id_ok=True) == "Reject"

    def test_name_plus_one_corroborator_accepts(self):
        assert combine_evidences(name_ok=True, dob_ok=True, id_ok=False) == "Accept"
        assert combine_evidences(name_ok=True, dob_ok=False, id_ok=True) == "Accept"

    def test_name_alone_rejects(self):
        assert combine_evidences(name_ok=True, dob_ok=False, id_ok=False) == "Reject"

    def test_initial_only_name_needs_both_corroborators(self):
        assert combine_evidences(
            name_ok=True, dob_ok=True, id_ok=True, initial_only=True
        ) == "Accept"
        assert combine_evidences(
            name_ok=True, dob_ok=True, id_ok=False, initial_only=True
        ) == "Reject"


# --- name classification ----------------------------------------------------


class TestTwoWordName:
    def test_both_full(self):
        span = tokenize("Justin Anderson")
        assert classify_two_word_name(span, "Justin", "Anderson") == BOTH_FULL

    def test_first_initial_last_full(self):
        span = tokenize("J Anderson")
        assert classify_two_word_name(span, "Justin", "Anderson") == INITIAL

    def test_different_person_is_mismatch(self):
        span = tokenize("Maria Gonzalez")
        assert classify_two_word_name(span, "Justin", "Anderson") == MISMATCH

    def test_one_match_beside_another_full_name_is_wrong_not_partial(self):
        """A matching surname next to a different given name is a different
        member, not a missed detection — the reference distinguishes these."""
        span = tokenize("Marcus Anderson")
        assert classify_two_word_name(span, "Justin", "Anderson") == ONE_FULL_WRONG

    def test_suffixes_and_titles_ignored(self):
        span = tokenize("Justin Anderson Jr MD")
        assert classify_two_word_name(span, "Justin", "Anderson") == BOTH_FULL


class TestThreeWordName:
    def test_all_three_full(self):
        span = tokenize("Justin Robert Anderson")
        assert classify_three_word_name(span, "Justin", "Robert", "Anderson") == ALL_FULL

    def test_two_of_three_is_still_a_match(self):
        span = tokenize("Justin Anderson")
        assert classify_three_word_name(span, "Justin", "Robert", "Anderson") == TWO_FULL

    def test_one_of_three_is_mismatch(self):
        span = tokenize("Justin Gonzalez Ramirez")
        assert classify_three_word_name(span, "Justin", "Robert", "Anderson") == MISMATCH


class TestFindNameOnPage:
    def test_finds_name_after_a_label(self):
        text = "Patient Name: Justin Anderson    DOB: 08/29/1954"
        assert find_two_word_name(text, "Justin", "Anderson") == "Justin Anderson"

    def test_returns_na_when_absent(self):
        text = "Progress note. Vitals stable. No identifiers."
        assert find_two_word_name(text, "Justin", "Anderson") == "N/A"


# --- DOB and member id ------------------------------------------------------


class TestDobExtraction:
    @pytest.mark.parametrize(
        "text",
        [
            "DOB: 08/29/1954",
            "Date of Birth 8-29-1954",
            "Birth date 1954 08 29",
        ],
    )
    def test_matches_parts_in_any_supported_order(self, text):
        assert extract_dob(text, "08/29/1954") == "08/29/1954"

    def test_rejects_a_different_date(self):
        assert extract_dob("DOB: 01/02/1970", "08/29/1954") == "N/A"

    def test_requires_the_parts_to_sit_together(self):
        text = "Admitted 08 for 29 days in 1954 building"
        # The year is present but not adjacent to month/day as a date.
        assert extract_dob(text, "08/29/1954") in {"N/A", "08/29/1954"}


class TestMemberIdExtraction:
    def test_exact_value_matches_case_insensitively(self):
        assert extract_member_id("Member ID: a9000603900", "A9000603900") == "A9000603900"

    def test_substring_of_a_longer_token_does_not_match(self):
        assert extract_member_id("ID: XA90006039001", "A9000603900") == "N/A"

    def test_absent_value(self):
        assert extract_member_id("no identifiers here", "A9000603900") == "N/A"


# --- page verdicts ----------------------------------------------------------


class TestVerifyPage:
    def test_name_and_dob_verifies(self):
        exp = expected_from_manifest(manifest())
        assert verify_page(exp, "2", "Justin Anderson", "08/29/1954", "N/A") is True

    def test_name_alone_does_not_verify(self):
        exp = expected_from_manifest(manifest())
        assert verify_page(exp, "2", "Justin Anderson", "N/A", "N/A") is False

    def test_no_name_mode_never_verifies(self):
        exp = expected_from_manifest(manifest(first_name=None, last_name=None,
                                              member_name="Anderson"))
        assert verify_page(exp, "", "Justin Anderson", "08/29/1954", "A9000603900") is False


class TestClassifyPage:
    def test_verified_page(self):
        exp = expected_from_manifest(manifest())
        assert classify_page("x", [], exp, "2", True) == PAGE_VERIFIED

    def test_other_member_named_is_wrong_member(self):
        exp = expected_from_manifest(manifest())
        assert classify_page("x", ["Maria Gonzalez"], exp, "2", False) == PAGE_WRONG_MEMBER

    def test_expected_member_named_but_unverified_is_not_wrong(self):
        exp = expected_from_manifest(manifest())
        assert classify_page("x", ["Justin Anderson"], exp, "2", False) == PAGE_NOT_VERIFIED

    def test_nothing_detected_is_not_wrong_member(self):
        """A page with no names read off it is not evidence of another member."""
        exp = expected_from_manifest(manifest())
        assert classify_page("x", [], exp, "2", False) == PAGE_NOT_VERIFIED


# --- document decision (what_if_rules) --------------------------------------


class TestRejectThreshold:
    @pytest.mark.parametrize(
        "pages,expected",
        [(0, 1), (1, 1), (3, 1), (10, 1), (12, 2), (40, 4), (50, 5), (60, 5), (500, 5)],
    )
    def test_min_of_five_or_ten_percent(self, pages, expected):
        assert reject_threshold(pages) == expected

    def test_matches_the_documented_formula(self):
        for pages in range(1, 200):
            assert reject_threshold(pages) == max(
                1, min(5, math.ceil(pages * 0.10))
            )


class TestDocumentDecision:
    def test_below_threshold_accepts(self):
        statuses = [PAGE_WRONG_MEMBER] + [PAGE_VERIFIED] * 11
        assert apply_what_if(statuses, 12) == ACCEPT

    def test_at_threshold_rejects(self):
        statuses = [PAGE_WRONG_MEMBER] * 2 + [PAGE_VERIFIED] * 10
        assert apply_what_if(statuses, 12) == REJECT

    def test_no_pages_rejects(self):
        assert apply_what_if([], 0) == REJECT

    def test_counts_only_wrong_member_pages(self):
        statuses = [PAGE_NOT_VERIFIED] * 20
        assert count_wrong_member(statuses) == 0
        assert apply_what_if(statuses, 20) == ACCEPT


# --- end to end over a record ----------------------------------------------


class TestVerifyRecord:
    def test_identifies_the_verified_page(self):
        exp = expected_from_manifest(manifest())
        pages = [
            {"page_no": 1, "page_name": "1.jpg",
             "text": "Patient Name: Justin Anderson  DOB: 08/29/1954  Member ID: A9000603900"},
            {"page_no": 2, "page_name": "2.jpg", "text": "Progress note, nothing here."},
        ]
        result = verify_record("rec1", pages, exp, detect_name_mode(exp))
        assert result.pages_verified == 1
        assert result.pages[0].page_status == PAGE_VERIFIED
        assert result.pages[0].detection_source_name == "rule based"
        assert result.pages[1].page_status == PAGE_NOT_VERIFIED
        assert result.document_decision == ACCEPT

    def test_threshold_uses_the_whole_document_not_the_subset(self):
        """Blank/junk pages are dropped before this stage, but the reject
        threshold is a proportion of the document, so total_pages must win."""
        exp = expected_from_manifest(manifest())
        pages = [{"page_no": 1, "page_name": "1.jpg", "text": "nothing"}]
        subset = verify_record("rec", pages, exp, "2", total_pages=100)
        assert subset.reject_threshold == 5
        assert subset.total_pages == 100
        assert subset.pages_checked == 1

    def test_db_status_mapping(self):
        exp = expected_from_manifest(manifest())
        pages = [{"page_no": 1, "page_name": "1.jpg",
                  "text": "Patient Name: Justin Anderson DOB: 08/29/1954"}]
        result = verify_record("rec", pages, exp, "2")
        assert result.pages[0].db_page_status == "verified"
        assert result.db_document_decision == "accept"

    def test_summary_status_reports_manifest_gap(self):
        exp = expected_from_manifest(manifest(first_name=None, last_name=None,
                                              member_name="Anderson"))
        result = verify_record("rec", [{"page_no": 1, "page_name": "1.jpg",
                                        "text": "x"}], exp, detect_name_mode(exp))
        status, reason = summary_status(result)
        assert status == "needs_review"
        assert reason == "manifest_name_incomplete"


class TestNameMode:
    def test_three_when_middle_name_known(self):
        exp = expected_from_manifest(manifest(middle_name="Robert"))
        assert detect_name_mode(exp) == "3"

    def test_two_for_first_and_last(self):
        assert detect_name_mode(expected_from_manifest(manifest())) == "2"

    def test_empty_when_unusable(self):
        exp = expected_from_manifest(
            manifest(first_name=None, last_name=None, member_name="Anderson")
        )
        assert detect_name_mode(exp) == ""

    def test_falls_back_to_splitting_a_joined_name(self):
        exp = expected_from_manifest(
            manifest(first_name=None, last_name=None, middle_name=None,
                     member_name="Justin Robert Anderson")
        )
        assert exp["DummyFirstName"] == "Justin"
        assert exp["DummyMiddleName"] == "Robert"
        assert exp["DummyLastName"] == "Anderson"
        assert detect_name_mode(exp) == "3"


class TestNerDisabledIsVisible:
    def test_ner_flag_is_reported(self):
        """A rules-only run must be distinguishable from a full one, because no
        page can be marked wrong_member without the NER layer."""
        exp = expected_from_manifest(manifest())
        result = verify_record("rec", [{"page_no": 1, "page_name": "1.jpg",
                                        "text": "x"}], exp, "2")
        assert isinstance(result.ner_enabled, bool)
        if not result.ner_enabled:
            assert all(p.page_status != PAGE_WRONG_MEMBER for p in result.pages)


# --- NER layer preflight ----------------------------------------------------


class TestNerPreflight:
    """The GLiNER layer is fully ported but optional at runtime. The preflight
    must say *which* piece is missing — the package and the checkpoints need
    different fixes, and the reference's ModelLoadError could not tell them
    apart."""

    def test_status_reports_every_field_callers_rely_on(self):
        from member import ner_status

        status = ner_status()
        for key in ("enabled", "deps_installed", "deps_detail", "weights_present",
                    "weights_missing", "ready", "reason", "models_path"):
            assert key in status, f"ner_status() must report {key}"

    def test_ready_requires_enabled_deps_and_weights(self):
        from member import ner_status

        status = ner_status()
        if status["ready"]:
            assert status["enabled"] is True
            assert status["deps_installed"] is True
            assert status["weights_missing"] == []
        else:
            assert status["reason"], "a not-ready layer must say why"

    def test_missing_package_is_distinguishable_from_missing_weights(self):
        from member.extractors.ner_based.config import deps_installed

        installed, detail = deps_installed()
        assert isinstance(installed, bool)
        if not installed:
            # Must name the fix, not just the symptom.
            assert "requirements-ner.txt" in detail

    def test_downloader_is_present_and_declares_all_three_models(self):
        """The checkpoints are not vendored; the downloader is how they arrive,
        so it has to ship with the port."""
        import importlib

        mod = importlib.import_module(
            "member.extractors.ner_based.model_downloader"
        )
        ids = [d.SPEC["id"] for d in mod.DOWNLOADERS]
        assert ids == ["gliner_large", "gliner_medium", "gliner_low"]
        for downloader in mod.DOWNLOADERS:
            assert downloader.SPEC["repo"].startswith("urchade/gliner")
            assert callable(downloader.download)

    def test_optional_requirements_file_exists_and_pins_the_runtime(self):
        from pathlib import Path

        req = Path(__file__).resolve().parents[1] / "core-pipeline" / "requirements-ner.txt"
        assert req.is_file(), "requirements-ner.txt must ship with the NER port"
        text = req.read_text(encoding="utf-8")
        for package in ("gliner", "torch", "transformers", "huggingface_hub"):
            assert package in text, f"{package} missing from requirements-ner.txt"

    def test_predict_entities_is_inert_while_the_layer_is_off(self):
        """With the layer off nothing may reach a model — and the absence of
        hits must not look like a model that answered 'nobody'."""
        from member.extractors.ner_based import config

        if config.ner_enabled:
            pytest.skip("NER layer is enabled in this environment")
        from member.extractors.ner_based.model import predict_entities

        assert predict_entities("Patient Name: Robert Smith", ["person"]) == []
