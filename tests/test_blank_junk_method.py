"""Blank / junk method (stages/lib/blank_junk/method.py): canon scoring, voters, ladder, vote."""
from __future__ import annotations

import pytest

import stages.lib.blank_junk.stage  # noqa: F401  — puts the blank_junk lib on sys.path

import classify  # noqa: E402
import method  # noqa: E402
from method import Line, PageInputs, decide, score_text  # noqa: E402

FILLER = "the quick brown fox jumps over a lazy dog near the river bank today"
JUNK_TEXT = "Invoice number 4411 amount due now. Fax cover sheet, number of pages 3, intended recipient only. " + FILLER
CLINICAL_TEXT = "HPI:\nchest pain\nAssessment:\nangina\nPlan:\nstress test, metformin 500 mg daily\n" + FILLER


def bert(p_junk: float, label: str = "JUNK_INVOICE") -> dict:
    return {"p_keep": round(1.0 - p_junk, 4), "p_junk": p_junk, "top_junk_label": label}


def tfidf(p_junk: float) -> dict:
    return {"p_junk": p_junk}


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

    def test_r2_placeholder(self):
        verdict = run("This page intentionally left blank")
        assert (verdict.code, verdict.rule) == (classify.CODE_OTHERS, "R2")

    def test_r3_provider_signature_keeps_a_junk_looking_page(self):
        verdict = run(JUNK_TEXT, signed=True, bert=bert(0.99), tfidf=tfidf(0.99))
        assert (verdict.code, verdict.rule) == (classify.CODE_MAIN, "R3")

    def test_r3_waits_for_pass_2(self):
        verdict = run(JUNK_TEXT, pass_no=1, signed=True, bert=bert(0.99), tfidf=tfidf(0.99))
        assert verdict.rule != "R3"

    def test_r4_distilbert_above_85_forces_junk(self):
        verdict = run(FILLER, bert=bert(0.90, "JUNK_LETTER_FAX"), tfidf=tfidf(0.1))
        assert (verdict.code, verdict.rule) == (classify.CODE_LETTER_FAX, "R4")
        assert verdict.confidence == pytest.approx(0.90)

    def test_r4_leaves_a_clinical_page_to_the_vote(self):
        text = "Assessment:\nstable\nPlan:\nrest " + FILLER  # clinical 4
        verdict = run(text, bert=bert(0.99), tfidf=tfidf(0.1))
        assert verdict.rule == "R9" and verdict.code == classify.CODE_MAIN
        assert verdict.votes["r4_skipped"] == "clinical"

    def test_r4_leaves_a_face_sheet_to_the_vote(self):
        text = "Patient Demographics face sheet guarantor emergency contact date of birth " + FILLER
        verdict = run(text, bert=bert(1.0), tfidf=tfidf(0.1))
        assert verdict.code == classify.CODE_MAIN and verdict.votes["r4_skipped"] == "demographic"

    def test_r4_needs_more_than_85(self):
        assert run(CLINICAL_TEXT, bert=bert(0.85)).rule != "R4"

    def test_r4_is_not_used_in_pass_1(self):
        assert run(CLINICAL_TEXT, pass_no=1, bert=bert(0.95)).rule != "R4"

    def test_r5_short_page_is_junk_even_with_a_clinical_heading(self):
        lines = [Line("PROGRESS NOTE", 0.03, 0.05), Line("Assessment and Plan:", 0.08, 0.10)]
        verdict = run("PROGRESS NOTE\nAssessment and Plan:", lines=lines, body_ink=False)
        assert (verdict.code, verdict.rule) == (classify.CODE_OTHERS, "R5")

    def test_r5_ignores_a_page_number_footer(self):
        lines = [Line("Patient: J Doe DOB 01/02/1950 visit", 0.03, 0.06), Line("Page 3 of 7", 0.96, 0.98)]
        assert run("Patient: J Doe DOB 01/02/1950 visit\nPage 3 of 7", lines=lines, body_ink=False).rule == "R5"

    def test_r5_does_not_fire_with_ink_below_or_unmeasured(self):
        lines = [Line("Wound care photo", 0.03, 0.05)]
        assert run("Wound care photo and notes", lines=lines, body_ink=True).rule != "R5"
        assert run("Wound care photo and notes", lines=lines, body_ink=None).rule != "R5"

    def test_r5_content_past_15_percent_is_not_short(self):
        lines = [Line("header", 0.03, 0.05), Line("more text", 0.20, 0.22)]
        assert run("header more text here", lines=lines, body_ink=False).rule != "R5"

    def test_r3_signature_beats_r5(self):
        lines = [Line("Electronically signed by A. Smith, MD", 0.03, 0.05)]
        assert run("Electronically signed by A. Smith, MD 03/14/24", lines=lines, body_ink=False).rule == "R3"

    def test_r6_retrieval_wording(self):
        verdict = run("Request for medical records from Datavant for the dates of service requested. " + FILLER)
        assert (verdict.code, verdict.rule) == (classify.CODE_RECORD_REQUEST, "R6")

    def test_r7_strong_clinical_keeps(self):
        verdict = run(CLINICAL_TEXT + "\nChief Complaint:\nchest pain", bert=bert(0.7), tfidf=tfidf(0.9))
        assert (verdict.code, verdict.rule) == (classify.CODE_MAIN, "R7")

    def test_r8_visual_is_kept(self):
        verdict = run(FILLER, document_type="visual", bert=bert(0.8), tfidf=tfidf(0.9))
        assert (verdict.code, verdict.rule) == (classify.CODE_MAIN, "R8")

    def test_r8_visual_with_invoice_wording_is_junk(self):
        text = "Invoice number 12, amount due, remit to, balance due " + FILLER
        verdict = run(text, document_type="visual")
        assert (verdict.code, verdict.rule) == (classify.CODE_INVOICE, "R8")


class TestVote:
    def test_three_strong_junk_ballots(self):
        verdict = run(JUNK_TEXT, bert=bert(0.84), tfidf=tfidf(0.95))
        # bert 0.84 is weak, so uncertain; kw is J+ (invoice + fax cover)
        assert verdict.code == classify.CODE_INVOICE and verdict.votes["row"] == 1 and verdict.uncertain
        assert verdict.confidence <= 0.60

    def test_two_strong_junk_and_a_weak_keep_is_junk(self):
        verdict = run(FILLER, pass_no=1, bert=bert(0.95), tfidf=tfidf(0.95))
        assert verdict.votes["ballots"] == "AJJ" and verdict.votes["row"] == 3
        # Who said what, in the votes and in the reason.
        assert sorted(verdict.votes["by_voter"].values()) == ["A", "J", "J"]
        assert set(verdict.votes["by_voter"]) == {"bert", "tfidf", "kw"}
        assert "DistilBERT " in verdict.reason and "TF-IDF " in verdict.reason and "keywords " in verdict.reason

    def test_a_strong_keep_blocks_two_junk_votes(self):
        # kw abstains on filler; give it clinical-weak text so it votes KEEP
        text = "Assessment:\nstable\nPlan:\nrest " + FILLER
        verdict = run(text, bert=bert(0.80), tfidf=tfidf(0.70))
        assert verdict.votes["row"] == 2 and verdict.code == classify.CODE_MAIN

    def test_two_weak_junk_and_a_silent_third_stays_main(self):
        verdict = run(FILLER, bert=bert(0.70), tfidf=tfidf(0.70))
        assert (verdict.votes["row"], verdict.code, verdict.uncertain) == (3, classify.CODE_MAIN, True)

    def test_two_junk_one_strong_and_a_silent_third_is_junk(self):
        verdict = run(FILLER, bert=bert(0.70), tfidf=tfidf(0.90))
        assert (verdict.votes["row"], verdict.code) == (3, classify.CODE_INVOICE)  # DistilBERT voted JUNK: its label
        # mean p(junk) 0.80, capped at 0.60: one of the two junk votes is weak
        assert verdict.confidence == pytest.approx(0.60) and verdict.uncertain

    def test_nothing_to_judge_is_main_with_low_confidence(self):
        verdict = run(FILLER, bert=bert(0.55), tfidf=tfidf(0.5))
        assert (verdict.votes["row"], verdict.code, verdict.uncertain) == (10, classify.CODE_MAIN, True)
        assert verdict.confidence <= 0.60

    def test_a_missing_model_abstains_and_lowers_the_confidence(self):
        verdict = run(FILLER, tfidf=tfidf(0.1))
        assert verdict.votes["bert"]["note"] == "unavailable" and verdict.uncertain

    def test_clinical_veto(self):
        text = JUNK_TEXT + "\nAssessment:\nstable\nPlan:\nrest"
        verdict = run(text, bert=bert(0.84), tfidf=tfidf(0.95))
        assert verdict.code == classify.CODE_MAIN
        assert verdict.votes.get("veto") in (None, "clinical")  # kw votes KEEP on C, or the veto fires

    def test_a_clear_call_keeps_its_confidence(self):
        verdict = run(FILLER, bert=bert(0.02), tfidf=tfidf(0.02))
        assert verdict.code == classify.CODE_MAIN and not verdict.uncertain
        assert verdict.confidence == pytest.approx(0.98)

    def test_handwritten_veto_above_30_percent(self):
        verdict = run(JUNK_TEXT, bert=bert(0.84), tfidf=tfidf(0.95), hw_area_pct=45.0)
        assert (verdict.code, verdict.votes.get("veto")) == (classify.CODE_MAIN, "handwritten")

    def test_no_handwritten_veto_at_30_percent_or_less(self):
        verdict = run(JUNK_TEXT, bert=bert(0.84), tfidf=tfidf(0.95), hw_area_pct=30.0)
        assert verdict.code == classify.CODE_INVOICE

    def test_pass_1_only_takes_a_unanimous_strong_junk(self):
        weak = run(JUNK_TEXT, pass_no=1, bert=bert(0.84), tfidf=tfidf(0.95))
        assert weak.code == classify.CODE_MAIN and "deferred" in weak.reason
        strong = run(JUNK_TEXT, pass_no=1, bert=bert(0.95), tfidf=tfidf(0.95))
        assert strong.code == classify.CODE_INVOICE

    def test_every_verdict_records_its_ballots(self):
        verdict = run(JUNK_TEXT, bert=bert(0.84), tfidf=tfidf(0.95))
        assert {"bert", "tfidf", "kw", "rule", "ballots", "row"} <= set(verdict.votes)
        assert verdict.votes["kw"]["junk_group"] == "invoice"


class TestPatientEducation:
    HANDOUT = (
        "Dim lighting Loose rug Clutter. This picture shows some of the things that can cause a fall in "
        "your home. Consumer Information Use and Disclaimer. This generalized information is a limited "
        "summary of diagnosis, treatment, and/or medication information."
    )

    def test_a_handout_is_clinical_so_distilbert_cannot_force_it(self):
        verdict = run(self.HANDOUT, bert=bert(0.99, "JUNK_RECORD_REQUEST"), tfidf=tfidf(0.0))
        assert verdict.code == classify.CODE_MAIN
        assert verdict.votes["r4_skipped"] == "clinical"

    def test_after_visit_summary_wording_counts(self):
        scores = score_text("After Visit Summary. Discharge instructions: call your doctor if the fever returns.")
        assert scores.group("clinical", "patient_education") >= 4


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
