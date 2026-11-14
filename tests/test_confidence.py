"""One confidence scale for every module (core-pipeline/modules/confidence.py)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from modules import confidence as conf


def test_bands_good_passable_review():
    assert [conf.band(v) for v in (1.0, 0.8, 0.79, 0.5, 0.49, None)] == [
        "good", "good", "passable", "passable", "review", None]
    assert [conf.level(v) for v in (0.9, 0.6, 0.2)] == ["high", "medium", "low"]
    assert conf.needs_review(0.49) and not conf.needs_review(0.5) and not conf.needs_review(None)


def test_the_one_rule_helpers_2026_11_14():
    """CONFIDENCE_LOGIC.md section 12: the model's own p, fixed lows for backup / flipped,
    anchor x 0.90 when carried, 1.0 for a measured fact, 0.95 for a pass-1 text rule."""
    assert conf.chosen(0.63) == 0.63 and conf.chosen(None) is None and conf.chosen(1.4) == 1.0
    assert conf.backup() == 0.4 and conf.flipped() == 0.3
    assert conf.needs_review(conf.backup()) and conf.needs_review(conf.flipped())
    assert conf.flipped() < conf.backup()  # a flipped value sorts first in review
    assert conf.carried(0.9) == pytest.approx(0.81) and conf.carried(None) is None
    assert conf.carried(conf.backup()) == pytest.approx(0.36) and conf.carried(conf.flipped()) == pytest.approx(0.27)
    assert conf.measured() == 1.0 and conf.pass1_rule() == 0.95


def test_the_rule_numbers_live_in_the_canon(monkeypatch):
    found = {**conf.DEFAULTS, "backup_low": 0.35, "flipped_low": 0.25, "carried_factor": 0.8,
             "duplicate": {**conf.DEFAULTS["duplicate"], "floor": 0.2}}
    monkeypatch.setattr(conf, "settings", lambda: found)
    assert conf.backup() == 0.35 and conf.flipped() == 0.25 and conf.carried(0.5) == pytest.approx(0.4)
    assert conf.duplicate_match(0.9) == pytest.approx(0.2)


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


class _Staged:
    def __init__(self, rows):
        self._rows = rows

    def rows(self, field_id):
        return self._rows.get(field_id) or []


def test_member_name_dob_and_id_each_have_their_own_confidence():
    """2026-11-14 (user): three separate confidences, each the KV model_score of the row it came
    from; rules only, NER and the manifest text search are backups; a row the model turned down
    is flipped. The page's column keeps the lowest."""
    from modules.member.stage import _page_confidence, member_field_confidences

    staged = _Staged({
        "name": [{"value": "Carl Rogers", "accepted": True, "selected": True, "model_score": 0.93,
                  "rule_accepted": True, "score": 0.8}],
        "dob": [{"value": "01/02/1950", "accepted": False, "model_score": 0.2, "rule_accepted": True, "score": 0.9}],
        "member_id": [{"value": "X123", "accepted": True, "score": 0.9}],  # rules only, no model
    })
    page = _member_page("verified", detected_name="Carl Rogers", detection_source_name="rule based",
                        detected_dob="01/02/1950", detection_source_dob="rule based",
                        detected_member_id="X123", detection_source_member_id="rule based")
    assert member_field_confidences(page, staged) == {"name": 0.93, "dob": 0.3, "member_id": 0.4}
    assert _page_confidence(page, staged) == 0.3  # the lowest of the three
    ner = _member_page("verified", detected_name="Carl Rogers", detection_source_name="ner")
    assert member_field_confidences(ner, staged)["name"] == 0.4
    searched = _member_page("verified", detected_name="Carl Rogers", detection_source_name="rule based",
                            ner_key_source_name="sentence: Patient")
    assert member_field_confidences(searched, staged)["name"] == 0.4
    # A database row (rule_based / extracted_*) reads the same.
    row = {"extracted_name": "Carl Rogers", "detection_source_name": "rule_based", "extracted_dob": None}
    assert member_field_confidences(row, staged) == {"name": 0.93, "dob": None, "member_id": None}
    assert _page_confidence(_member_page("not_verified")) is None  # nothing read, nothing decided


def test_member_chart_value_is_an_agreement_rate():
    from modules.member.stage import _agreement_rate

    pages = [SimpleNamespace(db_page_status=s) for s in ("verified", "verified", "wrong_member", "not_verified")]
    result = SimpleNamespace(pages_checked=4, pages=pages)
    assert _agreement_rate(result, "verified") == 0.5
    assert _agreement_rate(result, "failed") == 0.25
    assert _agreement_rate(SimpleNamespace(pages_checked=0, pages=[]), "verified") is None


def test_final_values_take_the_anchor_pages_confidence_and_carry_it():
    """2026-11-14 (user): the anchor keeps its own; a carried page reads anchor x 0.90; a page
    rules 7 / 8 re-placed is flipped; member name / DOB / ID each their own."""
    from modules.imaging_final.stage import final_confidences, review_fields

    classification = {
        1: {"page_type": "Progress Note", "confidence": 0.9, "axes": {"l1": "Progress Note", "specialty_confidence": 0.4}},
        2: {"page_type": "Progress Note", "confidence": 0.95, "axes": {"l1": "Progress Note"}},
    }
    common = dict(
        page_type="Progress Note", decider=1, type_pages=[1, 2], specialty_page=1, dos_page=1,
        encounter_page=1, classification=classification, dos={1: {"confidence": 0.91}},
        encounters={1: {"encounter_type": "outpatient_f2f", "confidence": 0.68}}, provider_confidence=None,
        blank_junk={2: {"blank_junk_flag": "not_blank_junk", "confidence": 0.42}}, quality={2: {"hw_confidence": 0.97}},
    )
    out = final_confidences(2, members={2: {"confidence": 0.84, "extracted_name": "A B"}},
                            member_fields={"name": 0.93, "dob": 0.3, "member_id": None}, **common)
    assert out == {"page_type": 0.81, "codability": 0.81, "specialty": 0.36, "dos": 0.819,
                   "encounter_type": 0.612, "member_name": 0.93, "member_dob": 0.3,
                   "blank_junk": 0.42, "handwriting": 0.97}
    assert review_fields(out) == ["blank_junk", "member_dob", "specialty"]
    # The anchor page keeps its own values.
    anchor = final_confidences(1, members={}, **common)
    assert anchor["page_type"] == 0.9 and anchor["dos"] == 0.91 and anchor["encounter_type"] == 0.68
    # A page rules 7 / 8 re-placed under the document's L1 is flipped.
    assert final_confidences(2, members={}, replaced=True, **common)["page_type"] == 0.3
    # Rows written before the split: the page's one member confidence for each value it has.
    old = final_confidences(2, members={2: {"confidence": 0.84, "extracted_name": "A B"}}, **common)
    assert old["member_name"] == 0.84 and "member_dob" not in old
    # 2026-11-02 (user): a document encounter type does not apply to is blank, confidence too.
    blank = final_confidences(
        2, **{**common, "encounters": {1: {"encounter_type": "not_applicable", "confidence": 0.9}}}, members={})
    assert blank.get("encounter_type") is None


def test_not_found_has_a_confidence():
    # Nothing the model scored: a backup (2026-11-14, section 12.3).
    assert conf.absent([]) == 0.4
    assert conf.absent([], "handwritten") == 0.4  # no penalties any more
    # A candidate turned down: 1 - its probability (a near miss is flagged).
    assert conf.absent([0.7, 0.2]) == pytest.approx(0.3) and conf.needs_review(conf.absent([0.7]))
    assert conf.absent([0.1, None]) == pytest.approx(0.9)


def test_staging_gives_each_field_without_an_accepted_row_its_absence():
    from modules.extraction.staging import StagedPage, absent_confidences

    fields = {"name": [{"accepted": True, "model_score": 0.9}],
              "dob": [{"accepted": False, "model_score": 0.4}], "member_id": [],
              "electronic_signature": [{"accepted": False, "score": 0.8}]}  # rules only: no model view
    assert absent_confidences(fields) == {"dob": pytest.approx(0.6), "member_id": 0.4, "electronic_signature": 0.4}
    # Always worked out on read, whatever an older staging stored.
    assert StagedPage("1.jpg", {"fields": fields, "absent": {"dob": 0.1}}).absent == absent_confidences(fields)


def test_orientation_and_tilt_confidence_from_osd_and_the_read():
    from types import SimpleNamespace

    from modules.image_preprocess.stage import _rotation_confidences

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
