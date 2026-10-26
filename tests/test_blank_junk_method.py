"""Blank / junk method (stages/lib/blank_junk/method.py): canon scoring, voters, ladder, vote."""
from __future__ import annotations

import pytest

import stages.lib.blank_junk.stage  # noqa: F401  — puts the blank_junk lib on sys.path

import classify  # noqa: E402
import method  # noqa: E402
from method import Line, PageInputs, decide, score_text  # noqa: E402

FILLER = "the quick brown fox jumps over a lazy dog near the river bank today"
JUNK_TEXT = "Invoice number 4411 amount due now. Fax cover sheet, number of pages 3, intended recipient only. " + FILLER
FAX_TEXT = "Fax cover sheet, number of pages 3. " + FILLER  # keywords: weak JUNK, score 4
INVOICE_TEXT = "Invoice number 4411, amount due, remit to, balance due " + FILLER  # strong JUNK, score 9
CLINICAL_WEAK = "Assessment:\nstable\nPlan:\nrest " + FILLER  # keywords: weak KEEP, clinical 4
CLINICAL_TEXT = "HPI:\nchest pain\nAssessment:\nangina\nPlan:\nstress test, metformin 500 mg daily\n" + FILLER


def bert(p_junk: float, label: str = "JUNK_INVOICE") -> dict:
    return {"p_keep": round(1.0 - p_junk, 4), "p_junk": p_junk, "top_junk_label": label}


def run(text: str = FILLER, pass_no: int = 2, **kw) -> method.Verdict:
    return decide(PageInputs(text=text, pass_no=pass_no, **kw))


class TestScoring:
    def test_a_term_repeated_counts_once(self):
        once = score_text("Datavant release. " + FILLER)
        many = score_text("Datavant Datavant Datavant release. " + FILLER)
        assert once.group("junk", "roi_vendors") == many.group("junk", "roi_vendors") == 4

    def test_two_different_terms_count_twice_up_to_the_cap(self):
        assert score_text("Datavant and Ciox").group("junk", "roi_vendors") == 8
        assert score_text("Datavant, Ciox and Verisma").group("junk", "roi_vendors") == 8  # cap

    def test_headings_count_only_at_the_start_of_a_line(self):
        assert score_text("Plan:\nrest").group("clinical", "section_headings") == 2
        assert score_text("we plan to follow up next week").group("clinical", "section_headings") == 0

    def test_the_longest_overlapping_phrase_wins(self):
        scores = score_text("FAX COVER SHEET")
        assert scores.hits.get("junk.fax_cover") == ["fax cover sheet"]
        assert "junk.cover_page" not in scores.hits

    def test_neutral_words_score_nothing(self):
        scores = score_text("Fax phone page printed confidential insurance subscriber policy number")
        assert (scores.clinical, scores.junk, scores.demographic) == (0, 0, 0)

    def test_a_junk_term_in_the_header_needs_body_junk(self):
        header = Line("fax cover sheet urgent", 0.02, 0.04)
        body = Line("Assessment: stable on current therapy", 0.30, 0.32)
        assert score_text("", [header, body]).junk == 0
        body_junk = Line("number of pages including cover", 0.30, 0.32)
        assert score_text("", [header, body_junk]).group("junk", "fax_cover") > 2


class TestRules:
    def test_r1_empty_text_is_blank(self):
        assert run("   ").code == classify.CODE_BLANK

    def test_r1_no_ink_is_blank(self):
        verdict = run(CLINICAL_TEXT, no_ink=True)
        assert (verdict.code, verdict.rule) == (classify.CODE_BLANK, "R1")

    def test_r1_does_not_blank_a_visual_page_with_no_text(self):
        assert run("", document_type="visual").code == classify.CODE_MAIN

    def test_r1_the_word_blank_under_30_characters_is_blank(self):
        verdict = run("Intentionally left blank")
        assert (verdict.code, verdict.rule) == (classify.CODE_BLANK, "R1")

    def test_r1_under_15_characters_is_junk_others(self):
        verdict = run("Page 3 of 7")
        assert (verdict.code, verdict.rule) == (classify.CODE_OTHERS, "R1")

    def test_r1_under_15_characters_with_a_cover_term_is_cover_page(self):
        verdict = run("COVER SHEET")
        assert (verdict.code, verdict.rule) == (classify.CODE_COVER_PAGE, "R1")


    def test_r6_does_not_fire_with_ink_below_or_unmeasured(self):
        lines = [Line("Wound care photo", 0.03, 0.05)]
        assert run("Wound care photo and notes", lines=lines, body_ink=True).rule != "R6"
        assert run("Wound care photo and notes", lines=lines, body_ink=None).rule != "R6"

    def test_r6_content_past_15_percent_is_not_short(self):
        lines = [Line("header", 0.03, 0.05), Line("more text", 0.20, 0.22)]
        assert run("header more text here", lines=lines, body_ink=False).rule != "R6"

    def test_r4_signature_beats_r5(self):
        assert run(FILLER, signed=True, signature_known=True, bert=bert(0.9)).rule == "R4"

    def test_r4_ignores_signature_phrases(self):
        # Only kv_extract's Electronic Signature counts; the words alone do not.
        verdict = run("Electronically signed by A. Smith, MD 03/14/24. " + FILLER, bert=bert(0.9))
        assert verdict.rule == "R5" and verdict.code != classify.CODE_MAIN


    def test_r3_visual_is_kept(self):
        verdict = run(FILLER, document_type="visual", bert=bert(0.8))
        assert (verdict.code, verdict.rule) == (classify.CODE_MAIN, "R3")

    def test_r3_visual_needs_card_wording_to_be_junk(self):
        text = "Invoice number 12, amount due, remit to, balance due"  # < 20 words: Visual
        verdict = run(text, document_type="visual")
        assert (verdict.code, verdict.rule) == (classify.CODE_MAIN, "R3")
        verdict = run(text + " member card", document_type="visual")
        assert (verdict.code, verdict.rule) == (classify.CODE_OTHERS, "R3")

    def test_r3_visual_needs_fewer_than_20_words(self):
        # 19 words: still Visual. 20 words: judged like any other page (the vote here).
        assert run(" ".join(["word"] * 19), document_type="visual", bert=bert(0.8)).rule == "R3"
        verdict = run(" ".join(["word"] * 20), document_type="visual", bert=bert(0.8))
        assert verdict.rule != "R3"
        assert "not Visual" in verdict.votes["visual"]
        assert method.text_is_visual("visual", "a b c") and not method.text_is_visual("printed", "a")


class TestGeometry:
    def test_words_group_into_lines(self):
        words = [
            {"content": "Page", "polygon": [10, 100, 50, 100, 50, 120, 10, 120]},
            {"content": "Header", "polygon": [60, 101, 120, 101, 120, 121, 60, 121]},
            {"content": "Body", "polygon": [10, 500, 50, 500, 50, 520, 10, 520]},
        ]
        lines = method.lines_from_words(words, 1000)
        assert [l.text for l in lines] == ["Page Header", "Body"]
        assert lines[1].bottom == pytest.approx(0.52)


def test_a_no_ink_page_is_judged_in_pass_1_and_is_blank():
    # The HW stage's blank_page / faint_marks_only pages are not "non-printed": pass 1
    # judges them (R1 Blank) and final OCR skips them. A handwritten page still waits.
    from db.memory_store import MemoryStore

    store = MemoryStore()
    for page_id, kind, hw in ((1, "uncertain", "blank_page"), (2, "uncertain", "faint_marks_only"),
                              (3, "handwritten", "convnext_page_tags")):
        store.upsert_quality(chart_id=7, page_id=page_id, printed_or_handwritten=kind, hw_method=hw)
    assert store.non_printed_page_ids(7) == {3}
    verdict = run("", pass_no=1, no_ink=True)
    assert (verdict.code, verdict.rule) == (classify.CODE_BLANK, "R1")


CARD_TEXT = (
    "Benefit Award Card # 8101 3360 3004 6207 For Pharmacist: 1-877-889-6510 "
    "Rx Claims: OptumRx UnitedHealthcare UCard Member ID 903710875-00 "
    "RxBIN 610097 RxPCN 9999 RxGRP COS Optum Health Networks"
)


def test_a_visual_card_with_card_wording_is_junk():
    verdict = run("UCard RxBIN 610097 RxPCN 9999 Card # 8101", document_type="visual")
    assert verdict.rule == "R3" and verdict.code != classify.CODE_MAIN


def test_a_drivers_licence_is_junk_evidence():
    assert score_text("Driver License DL # D1234567 class C organ donor", document_type="visual").best_junk_group() == "id_card"


def test_a_heading_line_counts_its_heading_word_anywhere():
    hits = score_text("Allergies and Adverse Reactions\nThis patient has no known allergies.").hits
    assert hits["clinical.section_headings"] == ["allergies"]
    # A long sentence is not a heading: the word inside it does not count.
    long = score_text("The patient was counselled about allergies and adverse reactions to the new drug today.")
    assert "clinical.section_headings" not in long.hits
    # A long line that starts with the heading and a colon still counts (the old rule).
    assert score_text("Allergies: penicillin, sulfa, codeine, latex, shellfish and peanuts").hits[
        "clinical.section_headings"
    ] == ["allergies"]


class TestVisualR3:
    def test_one_card_term_is_enough(self):
        verdict = run("Member card", document_type="visual")
        assert (verdict.rule, verdict.code) == ("R3", classify.CODE_OTHERS)

    def test_an_insurer_or_an_roi_vendor_alone_is_not_a_card(self):
        for text in ("Aetna", "Datavant", "Aetna Datavant Optum"):
            verdict = run(text, document_type="visual")
            assert (verdict.rule, verdict.code) == ("R3", classify.CODE_MAIN), text
        # With a card term they add weight (card 4 + insurer 2 + vendor 4); Junk / Others.
        verdict = run("Aetna Datavant member card", document_type="visual")
        assert (verdict.rule, verdict.code) == ("R3", classify.CODE_OTHERS)
        assert "(10)" in verdict.reason

    def test_card_numbers_count(self):
        for text in ("Group # 12345", "Member ID: W123456789", "PCP $25", "EXP 01/31/2027", "Card # 8101"):
            assert run(text, document_type="visual").rule == "R3", text
            assert run(text, document_type="visual").code == classify.CODE_OTHERS, text

    def test_card_check_comes_before_the_signature_and_clinical_rules(self):
        text = CLINICAL_TEXT + "\nChief Complaint:\nchest pain\nElectronically signed by A. Smith, MD\nMember ID: W123456789"
        verdict = run(text, document_type="visual", signed=True)
        assert (verdict.rule, verdict.code) == ("R3", classify.CODE_OTHERS)

    def test_a_card_is_always_junk_others(self):
        # Card 4 and vendor 4: Others, not Record Request.
        assert run("Optum member card", document_type="visual").code == classify.CODE_OTHERS

    def test_no_card_under_20_words_is_kept(self):
        verdict = run("ECG tracing lead II", document_type="visual", bert=bert(0.99))
        assert (verdict.rule, verdict.code) == ("R3", classify.CODE_MAIN)


    def test_card_wording_on_a_printed_page_is_not_r3(self):
        assert run("Aetna member card BIN: 610014 " + FILLER, document_type="printed").rule != "R3"


def test_pharmacy_wording_is_not_card_evidence():
    for text in ("RxBIN 610097 RxPCN 9999 RxGRP COS", "BIN: 610014", "For Pharmacist: 1-877-889-6510 Rx Claims"):
        verdict = run(text, document_type="visual")
        assert (verdict.rule, verdict.code) == ("R3", classify.CODE_MAIN), text


class TestDistilBertLadder:
    """2026-10-20: R1-R4, then DistilBERT alone; junk only in pass 2 except R1-R3."""

    def test_r2_placeholder_is_blank_not_junk(self):
        verdict = run("This page intentionally left blank", bert=bert(0.99))
        assert (verdict.code, verdict.rule) == (classify.CODE_BLANK, "R2")

    def test_pass_1_keeps_a_sure_page_for_good(self):
        verdict = run(FILLER, pass_no=1, bert=bert(0.02))
        assert (verdict.code, verdict.rule) == (classify.CODE_MAIN, "R5")
        assert method.is_final_keep(verdict.reason)
        assert verdict.confidence == pytest.approx(0.98)

    def test_pass_1_junks_only_what_distilbert_is_sure_of(self):
        # 2026-10-22 (user): p(junk) > 0.95 is junk in pass 1, for good.
        from stages._support import FINAL_JUNK

        verdict = run(INVOICE_TEXT, pass_no=1, bert=bert(0.99))
        assert (verdict.code, verdict.rule) == (classify.CODE_INVOICE, "R5")
        assert verdict.reason.startswith(FINAL_JUNK) and verdict.confidence == pytest.approx(0.99)
        for p in (0.95, 0.60, 0.10):
            verdict = run(INVOICE_TEXT, pass_no=1, bert=bert(p))
            assert verdict.code == classify.CODE_MAIN and verdict.rule == "P1"
            assert not verdict.reason.startswith(FINAL_JUNK) and verdict.votes["deferred"]

    def test_pass_1_does_not_junk_a_handwritten_page(self):
        from stages._support import FINAL_JUNK

        verdict = run(INVOICE_TEXT, pass_no=1, bert=bert(0.99), hw_area_pct=40.0)
        assert verdict.code == classify.CODE_MAIN and verdict.rule == "P1"
        assert not verdict.reason.startswith(FINAL_JUNK) and verdict.votes["veto"] == "handwritten"

    def test_pass_1_still_takes_r1_and_r3_junk(self):
        assert run("Page 3 of 7", pass_no=1, bert=bert(0.0)).rule == "R1"
        card = "Member ID card, Group number 0042, Datavant " + FILLER
        assert run(card, pass_no=1, document_type="visual", bert=bert(0.0)).rule == "R3"

    def test_pass_2_junk_cut_offs(self):
        sure = run(FILLER, bert=bert(0.90, "JUNK_LETTER_FAX"))
        assert (sure.code, sure.rule) == (classify.CODE_LETTER_FAX, "R5") and "review" not in sure.reason
        unsure = run(FILLER, bert=bert(0.60))
        assert unsure.code == classify.CODE_INVOICE and unsure.votes["review_required"]
        assert run(FILLER, bert=bert(0.49)).code == classify.CODE_MAIN

    def test_keywords_no_longer_decide(self):
        # Invoice wording, DistilBERT keeps: Main. Clinical wording, DistilBERT junks: Junk.
        assert run(INVOICE_TEXT, bert=bert(0.10)).code == classify.CODE_MAIN
        assert run(CLINICAL_TEXT, bert=bert(0.95)).code != classify.CODE_MAIN

    def test_a_signature_keeps_the_page_unless_distilbert_is_over_95(self):
        held = run(FILLER, signed=True, bert=bert(0.94))
        assert (held.code, held.rule) == (classify.CODE_MAIN, "R4")
        over = run(FILLER, signed=True, bert=bert(0.97))
        assert over.rule == "R5" and over.code != classify.CODE_MAIN and "r4_overruled" in over.votes

    def test_a_handwritten_page_is_not_junked_by_the_model(self):
        verdict = run(FILLER, bert=bert(0.95), hw_area_pct=45)
        assert verdict.code == classify.CODE_MAIN and verdict.votes["veto"] == "handwritten"

    def test_no_model_keeps_the_page_for_review(self):
        verdict = run(FILLER)
        assert verdict.code == classify.CODE_MAIN and verdict.votes["review_required"]


class TestShortPage:
    """R6 (pass 2, after R4, before R5): content in the top 15%, no ink below -> Junk / Others."""

    def short(self, **kw) -> method.Verdict:
        base = {"content_bottom": 0.10, "body_ink": False, "body_ink_fraction": 0.001}
        return run(FILLER, **{**base, **kw})

    def test_a_short_page_with_nothing_below_is_junk(self):
        verdict = self.short(bert=bert(0.30))
        assert (verdict.code, verdict.rule) == (classify.CODE_OTHERS, "R6")
        # DistilBERT's p(junk) 0.30 lifted by the rule (0.95), x 0.7 as DistilBERT says keep: review.
        assert verdict.confidence == pytest.approx((0.30 + 0.70 * 0.5 * 0.95) * 0.7, abs=1e-4)
        assert verdict.confidence < 0.5
        assert verdict.votes["short_page"]["content_bottom"] == 0.10

    def test_distilbert_sure_of_a_keep_skips_it(self):
        verdict = self.short(bert=bert(0.05))  # p(keep) 0.95 > 0.90
        assert verdict.rule == "R5" and verdict.code == classify.CODE_MAIN
        assert "skipped" in verdict.votes["short_page"]
        assert self.short(bert=bert(0.10)).rule == "R6"  # p(keep) 0.90 is not above 0.90

    def test_ink_below_or_not_measured_does_not_fire(self):
        assert self.short(bert=bert(0.30), body_ink=True, body_ink_fraction=0.02).rule == "R5"
        assert self.short(bert=bert(0.30), body_ink=None, body_ink_fraction=None).rule == "R5"

    def test_a_long_page_or_no_geometry_does_not_fire(self):
        assert self.short(bert=bert(0.30), content_bottom=0.40).rule == "R5"
        verdict = self.short(bert=bert(0.30), content_bottom=None)
        assert verdict.rule == "R5" and verdict.votes["short_page"] == "no_geometry"

    def test_a_signature_comes_first_and_pass_1_never_runs_it(self):
        assert self.short(bert=bert(0.30), signed=True).rule == "R4"
        assert run(FILLER, pass_no=1, content_bottom=0.10, body_ink=False, bert=bert(0.30)).rule == "P1"


class TestPostProcess:
    """2026-10-26 (user): a page with a signature or provider details is never junk; a DOS
    alone does not keep it (removed the same day)."""

    def test_plain_junk_and_pass_1_are_unchanged(self):
        for inputs in (PageInputs(text=JUNK_TEXT, pass_no=2, bert=bert(0.99)),
                       PageInputs(text=JUNK_TEXT, pass_no=1, bert=bert(0.99), signed=True)):
            verdict = decide(inputs)
            assert method.post_process(verdict, inputs) is verdict

    def test_signed_or_provider_pages_are_never_junk(self):
        for extra in ({"signed": True}, {"provider": True}):
            inputs = PageInputs(text=JUNK_TEXT, pass_no=2, bert=bert(0.99), **extra)
            verdict = method.post_process(decide(inputs), inputs)
            assert verdict.code == classify.CODE_MAIN and "never junk" in verdict.reason
