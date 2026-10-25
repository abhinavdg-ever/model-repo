"""One confidence scale for every module (core-pipeline/stages/lib/confidence.py)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from stages.lib import confidence as conf


def test_bands_good_passable_review():
    assert [conf.band(v) for v in (1.0, 0.8, 0.79, 0.5, 0.49, None)] == [
        "good", "good", "passable", "passable", "review", None]
    assert [conf.level(v) for v in (0.9, 0.6, 0.2)] == ["high", "medium", "low"]
    assert conf.needs_review(0.49) and not conf.needs_review(0.5) and not conf.needs_review(None)


def test_backing_keywords_lift_the_model_and_penalties_multiply():
    assert conf.backed(0.6, 1.0) == pytest.approx(0.8)  # half the gap to 1
    assert conf.backed(0.6, 0.0) == pytest.approx(0.6)
    assert conf.score(0.9, "keywords_disagree") == pytest.approx(0.63)
    assert conf.score(0.9, "conflict", "conflict") == pytest.approx(0.72)  # a reason counts once
    assert conf.score(1.0, "no_model", "fallback") == pytest.approx(0.51)
    assert conf.score(1.7) == 1.0 and conf.score(-0.2) == 0.0 and conf.score(None) is None


def test_under_the_modules_own_bar_reads_under_the_review_line():
    assert conf.under_bar(0.84, 0.85) < 0.5
    assert conf.under_bar(0.6, 0.85) < conf.under_bar(0.8, 0.85)


def test_calibration_knots_are_piecewise_linear(monkeypatch):
    monkeypatch.setattr(conf, "settings", lambda: {**conf.DEFAULTS, "calibration": {"x": [[0, 0], [0.5, 0.8], [1, 1]]}})
    assert conf.calibrate("x", 0.25) == pytest.approx(0.4)
    assert conf.calibrate("x", 0.75) == pytest.approx(0.9)
    assert conf.calibrate("other", 0.25) == 0.25


def test_page_reasons():
    assert conf.page_reasons("x" * 500, "handwritten") == ("handwritten",)
    assert conf.page_reasons("short", "printed") == ("thin_text",)
    assert conf.page_reasons(None, None) == ()


def _member_page(status, **found):
    fields = {"detected_name": "", "detected_dob": "", "detected_member_id": "",
              "detection_source_name": "", "detection_source_dob": "", "detection_source_member_id": ""}
    fields.update(found)
    return SimpleNamespace(db_page_status=status, **fields)


def test_member_confidence_is_the_evidence_found():
    from stages.lib.member.stage import _page_confidence

    name_dob = _member_page("verified", detected_name="A B", detection_source_name="rule based",
                            detected_dob="01/01/1950", detection_source_dob="rule based")
    assert _page_confidence(name_dob) == pytest.approx(0.72)  # 0.6, backed by the other 0.6
    # The extraction's own confidences (extraction.json) when it read them: they differ per page.
    assert _page_confidence(name_dob, extracted={"name": 0.8, "dob": 0.97}) == pytest.approx(0.97 + 0.03 * 0.5 * 0.8)
    assert _page_confidence(_member_page("verified", detected_name="A B", detection_source_name="ner")) == 0.4
    assert _page_confidence(_member_page("not_verified")) is None  # nothing read, nothing decided
    unmatched = _member_page("not_verified", detected_name="A B", detection_source_name="rule based")
    assert _page_confidence(unmatched) == pytest.approx(0.36)  # 0.6 x fallback


def test_final_values_take_the_deciding_pages_confidence_and_flag_the_low_ones():
    from stages.lib.imaging_final.stage import final_confidences, review_fields

    classification = {
        1: {"page_type": "Progress Note", "confidence": 0.9, "axes": {"l1": "Progress Note", "specialty_confidence": 0.4}},
        2: {"page_type": "Lab Report", "confidence": 0.95, "axes": {"l1": "Lab Report"}},
    }
    out = final_confidences(
        2, page_type="Progress Note", decider=1, type_pages=[1, 2], specialty_page=1, dos_page=1,
        encounter_page=1, classification=classification, dos={1: {"confidence": 0.91}},
        encounters={1: {"confidence": 0.68}}, members={2: {"confidence": 0.84}}, provider_confidence=None,
        blank_junk={2: {"blank_junk_flag": "not_blank_junk", "confidence": 0.42}}, quality={2: {"hw_confidence": 0.97}},
    )
    # Page 2 is sure it is a Lab Report: the document's Progress Note reads 0.9 x conflict.
    assert out == {"page_type": 0.72, "codability": 0.72, "specialty": 0.4, "dos": 0.91,
                   "encounter_type": 0.68, "member": 0.84, "blank_junk": 0.42, "handwriting": 0.97}
    # A page kept Main on a weak verdict is flagged too.
    assert review_fields(out) == ["blank_junk", "specialty"]


def test_not_found_has_a_confidence():
    # Nothing found at all: 1.0 x no_model; on a handwritten page x handwritten too.
    assert conf.absent([]) == pytest.approx(0.85)
    assert conf.absent([], "handwritten") == pytest.approx(0.765)
    # A candidate turned down: 1 - its probability (a near miss is flagged).
    assert conf.absent([0.7, 0.2]) == pytest.approx(0.3) and conf.needs_review(conf.absent([0.7]))
    assert conf.absent([0.1, None]) == pytest.approx(0.9)


def test_staging_gives_each_field_without_an_accepted_row_its_absence():
    from stages.lib.extraction.staging import StagedPage, absent_confidences

    fields = {"name": [{"accepted": True, "model_score": 0.9}],
              "dob": [{"accepted": False, "model_score": 0.4}], "member_id": []}
    assert absent_confidences(fields) == {"dob": pytest.approx(0.6), "member_id": pytest.approx(0.85)}
    # Staging written before the absence existed works it out on read.
    assert StagedPage("1.jpg", {"fields": fields}).absent == absent_confidences(fields)


def test_a_page_with_no_member_read_takes_the_absence():
    from stages.lib.member.stage import _page_confidence

    assert _page_confidence(_member_page("not_verified"), 0.6) == 0.6


def test_orientation_and_tilt_confidence_from_osd_and_the_read():
    from types import SimpleNamespace

    from stages.lib.image_preprocess.stage import _rotation_confidences

    read = SimpleNamespace(share=0.9, tokens=120)
    turn, tilt = _rotation_confidences("osd", 6.0, read, 1.0)
    assert 0.86 < turn < 1.0 and tilt == pytest.approx(0.9)  # OSD 0.86, backed by a page that reads
    assert _rotation_confidences("readability", 6.0, read, 1.0)[0] == pytest.approx(0.63)  # 0.9 x overturned
    turn, tilt = _rotation_confidences("osd_undecided", 0.0, None, 0.0)
    assert turn == pytest.approx(0.3) and tilt == pytest.approx(0.3)  # nothing read: 0.5 x fallback


def test_review_ui_subtype_confidence_is_the_deepest_named_level():
    from app.services.imaging_overlays import subtype_confidence

    assert subtype_confidence("Lab Result", "0.9", "Chemistry Report", "0.6") == 0.6
    assert subtype_confidence("Physical Exam", 0.24, None, None) == 0.24
    assert subtype_confidence("Others", 0.9, None, None) is None
