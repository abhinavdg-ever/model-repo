"""Tests for page type / codeable classification against codeable_canon.json."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CORE = REPO / "core-pipeline"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from stages.lib.page_classify.codeable_classify import (  # noqa: E402
    classify_pages,
    load_canon,
    score_text,
)


@pytest.fixture(scope="module")
def canon():
    load_canon.cache_clear()
    return load_canon()


def test_canon_has_continue_flags(canon):
    assert any(e.carries for e in canon)
    tags = {e.tag for e in canon}
    assert "codeable" in tags
    assert "non_codeable" in tags
    assert "discharge_frequency" in tags


def test_progress_note_scores_codeable(canon):
    hit = score_text("PROGRESS NOTE\nPatient presents with cough.", canon)
    assert hit is not None
    assert hit.tag == "codeable"
    assert hit.continue_ == "y"


def test_consent_form_scores_non_codeable(canon):
    hit = score_text("Consent form — patient authorization signature", canon)
    assert hit is not None
    assert hit.tag == "non_codeable"


def test_discharge_summary_scores_discharge(canon):
    hit = score_text("DISCHARGE SUMMARY\nHospital Course: ...", canon)
    assert hit is not None
    assert hit.tag == "discharge_frequency"
    assert hit.continue_ == "y"


def test_continue_carries_until_dos_changes(canon):
    pages = [
        {
            "page_id": 1,
            "page_name": "1.jpg",
            "page_number": 1,
            "text": "Progress Note visit today",
            "dos_from": "2024-01-10",
            "dos_to": "2024-01-10",
        },
        {
            "page_id": 2,
            "page_name": "2.jpg",
            "page_number": 2,
            "text": "random continuation page with no header keywords",
            "dos_from": "2024-01-10",
            "dos_to": "2024-01-10",
        },
        {
            "page_id": 3,
            "page_name": "3.jpg",
            "page_number": 3,
            "text": "Consent form authorization",
            "dos_from": "2024-02-01",
            "dos_to": "2024-02-01",
        },
    ]
    rows = classify_pages(pages, entries=canon)
    assert rows[0]["tag"] == "codeable"
    assert rows[0]["continue_applied"] == "n"
    assert rows[1]["tag"] == "codeable"
    assert rows[1]["continue_applied"] == "y"
    assert rows[1]["is_codeable"] == "Codeable"
    assert rows[2]["tag"] == "non_codeable"
    assert rows[2]["continue_applied"] == "n"


def test_continue_switches_on_new_continue_type_same_dos(canon):
    pages = [
        {
            "page_id": 1,
            "page_name": "1.jpg",
            "text": "Progress Note",
            "dos_from": "2024-03-01",
            "dos_to": "2024-03-01",
        },
        {
            "page_id": 2,
            "page_name": "2.jpg",
            "text": "Discharge Report final",
            "dos_from": "2024-03-01",
            "dos_to": "2024-03-01",
        },
        {
            "page_id": 3,
            "page_name": "3.jpg",
            "text": "no keywords here",
            "dos_from": "2024-03-01",
            "dos_to": "2024-03-01",
        },
    ]
    rows = classify_pages(pages, entries=canon)
    assert rows[0]["tag"] == "codeable"
    assert rows[1]["tag"] == "discharge_frequency"
    assert rows[1]["continue_applied"] == "n"
    assert rows[2]["tag"] == "discharge_frequency"
    assert rows[2]["continue_applied"] == "y"


def test_unmatched_page_is_not_sure(canon):
    pages = [
        {
            "page_id": 1,
            "page_name": "1.jpg",
            "page_number": 5,
            "text": "zzzz unrelated scribbles with no clinical headers",
            "dos_from": "",
            "dos_to": "",
        }
    ]
    rows = classify_pages(pages, entries=canon)
    assert rows[0]["page_type"] == "Not Available"
    assert rows[0]["tag"] == "not_sure"
    assert rows[0]["is_codeable"] == "Not Sure"


def test_early_pages_with_patient_data_are_demographics(canon):
    text = (
        "Patient Name: Jane Doe\nDate of Birth: 01/01/1980\n"
        "Address: 1 Main St\nMember ID: 12345\nInsurance: Aetna"
    )
    pages = [
        {
            "page_id": 1,
            "page_name": "1.jpg",
            "page_number": 1,
            "text": text,
            "dos_from": "",
            "dos_to": "",
        },
        {
            "page_id": 2,
            "page_name": "2.jpg",
            "page_number": 2,
            "text": text,
            "dos_from": "",
            "dos_to": "",
        },
    ]
    rows = classify_pages(pages, entries=canon)
    assert rows[0]["page_type"] == "Demographics"
    assert rows[0]["is_codeable"] == "Codeable"
    assert rows[1]["page_type"] == "Demographics"


def test_demographics_canon_entry_exists(canon):
    types = {e.page_type for e in canon}
    assert "Demographics" in types
    assert not any("\\" in e.page_type for e in canon)


def test_page_type_labels_hide_parentheticals(canon):
    assert not any("(" in e.page_type or ")" in e.page_type for e in canon)
    soap = next(e for e in canon if e.page_type.casefold().startswith("soap note"))
    assert soap.page_type == "SOAP Note"


def test_consultations_and_consulta_are_one_type(canon):
    types = {e.page_type for e in canon}
    assert "Consultations / Consulta" in types
    assert "Consultations" not in types
    assert "Consulta (Spanish)" not in types
    assert "Consulta" not in types
    hit = score_text("CONSULTA medica del paciente", canon)
    assert hit is not None
    assert hit.page_type == "Consultations / Consulta"
    hit2 = score_text("Consultations follow-up note", canon)
    assert hit2 is not None
    assert hit2.page_type == "Consultations / Consulta"


def test_strong_near_duplicates_merged(canon):
    types = {e.page_type for e in canon}
    assert "Progress Note" in types
    assert "Progress notes" not in types
    assert not any(t == "Progress notes" for t in types)

    assert "Initial Psychiatric Evaluation" in types
    assert not any(t.casefold().startswith("intial") for t in types)

    assert "Telephone Messages" in types
    assert "Telephone or Call - Messages" not in types

    assert "Any Type of Notice" in types
    assert "Any Type of Notification" not in types

    assert "PAP / HPV Report" in types
    assert "PAP Report/Pap Smear" not in types
    assert "PAP/HPV Report" not in types

    assert "Annual Wellness Visit" in types
    assert "Annual Wellnes Visit" not in types
    assert "Madicare Annual Wellness" not in types

    assert "Emergency / ED Visit" in types
    assert "Emegency visit records" not in types


def test_spelling_fixes_in_canon(canon):
    types = {e.page_type for e in canon}
    assert "Lab Requisition" in types
    assert "Principal Diagnosis" in types
    assert "Rehabilitation Daily Treatment Note" in types
    assert "Physical Therapy Assessment/Note" in types
    assert "Echocardiogram / Transthoracic Echocardiography" in types
    # Typo aliases still match
    hit = score_text("Lab requisation form attached", canon)
    assert hit is not None
    assert hit.page_type == "Lab Requisition"
    hit2 = score_text("intial psychiatric evaluation note", canon)
    assert hit2 is not None
    assert hit2.page_type == "Initial Psychiatric Evaluation"


def test_progress_note_family_wins_over_other_families(canon):
    text = (
        "PROGRESS NOTE\nOffice visit today.\nConsent form authorization.\n"
        "Patient Education handout attached."
    )
    hit = score_text(text, canon)
    assert hit is not None
    assert hit.family == "progress_note"
    assert hit.tag == "codeable"


def test_office_visit_beats_weaker_matches(canon):
    text = (
        "Office visit follow-up.\nPrescriptions/RX Page refill list.\n"
        "Check List of vitals."
    )
    hit = score_text(text, canon)
    assert hit is not None
    n = hit.page_type.casefold()
    assert "office visit" in n or n == "visit report"


# --- loader --------------------------------------------------------------


def _raw_canon():
    import json

    from stages.lib.page_classify.codeable_classify import CANON_PATH

    return json.loads(CANON_PATH.read_text(encoding="utf-8"))


def _entry(eid, primary, family="progress_note", **extra):
    return {"id": eid, "display": eid, "family": family, "continue": False,
            "match": {"primary": primary, "supporting": [], "variants": []}, **extra}


@pytest.mark.parametrize(
    "entries, message",
    [
        ([_entry("a", ["shared phrase"]), _entry("b", ["shared phrase"])], "claimed by"),
        ([_entry("a", ["summary"])], "one-word primary"),
        ([_entry("a", [])], "no primary"),
        ([_entry("a", ["two words"], family="nope")], "unknown family"),
        ([_entry("a", ["two words"], tag="non_codeable")], "belongs to the family"),
    ],
)
def test_loader_refuses_a_bad_canon(entries, message):
    from stages.lib.page_classify.codeable_classify import CanonError, _parse_canon

    raw = _raw_canon()
    raw["entries"] = entries
    with pytest.raises(CanonError, match=message):
        _parse_canon(raw)


def test_shipped_canon_loads(canon):
    assert len(canon) == 253
    assert {e.family for e in canon} <= set(canon.families)


# --- matching ------------------------------------------------------------


def test_keywords_match_on_word_boundaries(canon):
    from stages.lib.page_classify.codeable_classify import _entry_hits

    ems = next(e for e in canon if any(p.text == "ems" for p in e.phrases))
    assert not _entry_hits(ems, "systems and problems reviewed", canon.matching)
    assert _entry_hits(ems, "ems run sheet", canon.matching)


def test_a_title_in_the_header_outweighs_the_same_phrase_in_the_body(canon):
    body = " filler" * 200
    top = score_text("Discharge Summary" + body, canon)
    mid = score_text(body + " discharge summary" + body, canon)
    bottom = score_text(body + " discharge summary", canon)
    assert top.score == 2 * mid.score
    assert bottom.score == 0.5 * mid.score


def test_phrase_weight_is_capped(canon):
    from stages.lib.page_classify.codeable_classify import _entry_hits

    entry = next(e for e in canon if e.id == "face_to_face_evaluation_note")
    (hit,) = _entry_hits(entry, "x " * 100 + "face to face evaluation note" + " y" * 100,
                         canon.matching)
    assert hit.weight == canon.matching.phrase_weight[4]


def test_supporting_hits_alone_cannot_decide(canon):
    # "summary" alone used to tag a page as a therapy discharge note.
    assert score_text("summary of findings, assessment and objective", canon) is None


def test_confidence_is_the_margin_over_another_family(canon):
    body = " filler" * 200
    alone = score_text("Discharge Summary" + body, canon)
    assert alone.confidence == 1.0
    contested = score_text("Discharge Summary" + body + " consent form" + body, canon)
    assert contested.confidence == pytest.approx((8 - 4) / 8)


def test_same_family_tie_is_not_uncertainty(canon):
    hit = score_text("Progress Note / Office Visit", canon)
    assert hit.family == "progress_note"
    assert hit.confidence == 1.0


def test_telephone_encounter_matches_real_text(canon):
    hit = score_text("TELEPHONE ENCOUNTER\nPatient called about refill.", canon)
    assert hit.page_type.startswith("Telephone Encounter")


# --- spans ---------------------------------------------------------------


def _page(pid, text, dos="2024-03-01"):
    return {"page_id": pid, "page_name": f"{pid}.jpg", "page_number": pid,
            "text": text, "dos_from": dos, "dos_to": dos}


def test_span_is_keyed_on_family(canon):
    rows = classify_pages(
        [
            _page(1, "SOAP Note subjective objective"),
            _page(2, "Physician Notes continued"),
            _page(3, "continuation with no keywords"),
        ],
        entries=canon,
    )
    assert [r["family"] for r in rows] == ["progress_note"] * 3
    assert rows[1]["page_type"] == "Physician Notes"  # its own type, same family
    assert rows[1]["continue_applied"] == "y"
    assert rows[2]["page_type"].startswith("SOAP Note")  # the opener's type
    assert rows[2]["continue_applied"] == "y"


def test_span_needs_contiguous_dates(canon):
    rows = classify_pages(
        [
            _page(1, "Progress Note"),
            _page(2, "Consent form authorization", dos="2024-04-01"),
            _page(3, "no keywords at all"),
        ],
        entries=canon,
    )
    assert rows[2]["continue_applied"] == "n"
    assert rows[2]["tag"] == "not_sure"


def test_pages_without_a_date_share_no_span(canon):
    rows = classify_pages(
        [_page(1, "Progress Note", dos=""), _page(2, "no keywords at all", dos="")],
        entries=canon,
    )
    assert rows[1]["continue_applied"] == "n"


def test_the_dos_default_is_not_a_span_date():
    from stages.lib.page_classify.stage import _DOS_PROFILE, _page_dos

    default = _DOS_PROFILE.get()["DOS_DEFAULT_DATE"]
    assert _page_dos({"date_of_service_from_doclevel": default,
                      "date_of_service_to_doclevel": default}) == ("", "")
    assert _page_dos({"date_of_service_from_doclevel": "2024-03-01",
                      "date_of_service_to_doclevel": "2024-03-01"}) == ("2024-03-01", "2024-03-01")
    # A page that really says 2022-02-02 keeps it.
    assert _page_dos({"date_of_service_from": default, "date_of_service_to": default}) == (
        default, default)


def test_rows_carry_the_evidence(canon):
    (row,) = classify_pages([_page(1, "Discharge Summary consent form")], entries=canon)
    assert row["family_scores"]["discharge"] > row["family_scores"]["consent_authorization"]
    assert {h["role"] for h in row["hits"]} == {"primary"}
    assert row["previous_family"] == ""


def test_output_page_type_is_family_then_type(canon):
    from stages.lib.page_classify.stage import _output_page_type

    rows = classify_pages(
        [
            _page(1, "SOAP Note subjective", dos="2024-01-01"),
            _page(2, "Progress Note", dos="2024-02-01"),
            _page(3, "zzzz nothing", dos=""),
        ],
        entries=canon,
    )
    family = canon.families["progress_note"].display
    assert _output_page_type(rows[0]) == f"{family} (SOAP Note)"
    assert _output_page_type(rows[1]) == family  # same name, not "X (X)"
    assert _output_page_type(rows[2]) == "Not Available"


def test_type_probability_within_the_family(canon):
    pad = " filler" * 200
    hit = score_text(f"{pad} physician notes {pad} physician notes {pad} nurses notes {pad}",
                     canon)
    assert hit.family == "progress_note"
    assert hit.page_type == "Physician Notes"
    assert hit.type_confidence == pytest.approx(8 / 12, abs=1e-4)
    assert sum(hit.type_scores.values()) == pytest.approx(1.0, abs=1e-3)


def test_the_family_total_decides_not_the_single_best_type(canon):
    pad = " filler" * 200  # keep every hit in the body band
    text = f"{pad} discharge summary {pad} physician notes {pad} nurses notes {pad}"
    hit = score_text(text, canon)
    # One discharge type (4) against two progress-note types (4 + 4).
    assert hit.family_scores["discharge"] == 4
    assert hit.family_scores["progress_note"] == 8
    assert hit.family == "progress_note"
    assert hit.confidence == pytest.approx((8 - 4) / 8)


def test_a_phrase_two_types_share_counts_once_for_the_family(canon):
    pad = " filler" * 200
    hit = score_text(f"{pad} office visit {pad}", canon)
    # "office visit" is primary for one progress-note type, supporting for another.
    assert hit.family_scores["progress_note"] == 4


def test_loader_refuses_a_family_without_a_valid_tag():
    from stages.lib.page_classify.codeable_classify import CanonError, _parse_canon

    raw = _raw_canon()
    raw["families"]["progress_note"]["tag"] = "nope"
    with pytest.raises(CanonError, match="family progress_note: unknown tag"):
        _parse_canon(raw)


def test_no_family_mixes_codeable_and_non_codeable(canon):
    tags: dict[str, set[str]] = {}
    for e in canon:
        tags.setdefault(e.family, set()).add(e.tag)
    assert all(len(t) == 1 for t in tags.values())
    assert {e.tag for e in canon if e.family == "laboratory"} == {"non_codeable"}
    assert {e.tag for e in canon if e.family == "pathology"} == {"codeable"}
