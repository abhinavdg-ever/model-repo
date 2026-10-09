"""Continuation tags: signature closes a progress note; MiniLM judges the rest."""
from __future__ import annotations

from stages.lib.continuation.tag import BREAK_ANCHORS, CONTINUE_ANCHORS, tag_pages


def _encode_toward(texts):
    """Point a junction at the continuation anchors when it says CONTINUES."""
    vectors = []
    for text in texts:
        if text in CONTINUE_ANCHORS or "CONTINUES" in text:
            vectors.append([1.0, 0.0])
        elif text in BREAK_ANCHORS or "NEWDOC" in text:
            vectors.append([0.0, 1.0])
        else:
            vectors.append([0.5, 0.5])
    return vectors


def test_a_progress_note_stays_open_until_the_signature_page():
    pages = [
        {
            "text": "Progress note. The patient is here for follow up.",
            "family": "progress_note",
        },
        {
            "text": "Assessment: stable. Plan: continue current medicines.",
            "family": "progress_note",
        },
        {
            "text": "Electronically signed by Jane Chen, MD on 03/15/2024",
            "family": "progress_note",
        },
        {
            "text": "Laboratory report. All results are final.",
            "family": "laboratory",
        },
    ]
    tags = tag_pages(pages, encoder=None)
    assert [tag["continues_previous"] for tag in tags] == ["n", "y", "y", "n"]
    assert tags[1]["continue_reason"] == "progress_note"
    assert tags[2]["continue_reason"] == "progress_note"
    assert tags[3]["continue_reason"] != "progress_note"


def test_a_finished_sentence_does_not_close_an_unsigned_progress_note():
    pages = [
        {"text": "Office visit.\n\nThe visit is complete.", "family": "progress_note"},
        {"text": "Medication list follows on its own.", "family": "medication"},
    ]
    tags = tag_pages(pages, encoder=None)
    assert tags[1] == {"continues_previous": "y", "continue_reason": "progress_note"}


def test_stepping_page_numbers_continue():
    pages = [
        {"text": "History.\n\nPage 1 of 3", "family": ""},
        {"text": "Exam.\n\nPage 2 of 3", "family": ""},
    ]
    tags = tag_pages(pages, encoder=None)
    assert tags[1] == {"continues_previous": "y", "continue_reason": "page_number"}


def test_page_one_starts_a_new_document():
    pages = [
        {"text": "The plan is to continue the current regimen", "family": ""},
        {"text": "Face sheet\n\nPage 1 of 4", "family": ""},
    ]
    tags = tag_pages(pages, encoder=None)
    assert tags[1] == {"continues_previous": "n", "continue_reason": "new_document"}


def test_minilm_judges_a_junction_the_wording_rules_do_not():
    pages = [
        {"text": "Seen today.", "family": ""},
        {"text": "CONTINUES the same visit on this page.", "family": ""},
    ]
    rules = tag_pages(pages, encoder=None)
    judged = tag_pages(pages, encoder=_encode_toward)
    assert rules[1]["continue_reason"] != "minilm"
    assert judged[1] == {"continues_previous": "y", "continue_reason": "minilm"}


def test_minilm_can_call_a_new_document():
    pages = [
        {"text": "Seen today.", "family": ""},
        {"text": "NEWDOC registration form starts here.", "family": ""},
    ]
    tags = tag_pages(pages, encoder=_encode_toward)
    assert tags[1] == {"continues_previous": "n", "continue_reason": "minilm"}


def test_a_page_that_stops_mid_sentence_continues_without_minilm():
    pages = [
        {"text": "The patient was given acetaminophen and", "family": ""},
        {"text": "was told to return if the pain worsened.", "family": ""},
    ]
    tags = tag_pages(pages, encoder=None)
    assert tags[1]["continues_previous"] == "y"
    assert tags[1]["continue_reason"] in {"leftover", "mid_sentence"}
