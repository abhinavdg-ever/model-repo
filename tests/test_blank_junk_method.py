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

    def test_r2_placeholder(self):
        verdict = run("This page intentionally left blank")
        assert (verdict.code, verdict.rule) == (classify.CODE_OTHERS, "R2")

    def test_r3_is_skipped_on_jj(self):
        # DistilBERT 0.99 and the invoice wording (keywords JUNK): the signature does not save
        # it; the vote decides.
        verdict = run(INVOICE_TEXT, signed=True, bert=bert(0.99))
        assert verdict.rule == "R9" and verdict.code != classify.CODE_MAIN
        assert verdict.votes["r3_skipped"] == "JJ"
        # JJ with a weak keyword ballot: still the vote (row 1, one strong -> Junk).
        verdict = run(FAX_TEXT, signed=True, bert=bert(0.99))
        assert verdict.rule == "R9" and verdict.code != classify.CODE_MAIN

    def test_r3_holds_when_only_one_voter_says_junk(self):
        verdict = run(CLINICAL_WEAK, signed=True, bert=bert(0.99))  # keywords KEEP
        assert (verdict.code, verdict.rule) == (classify.CODE_MAIN, "R3")

    def test_r3_waits_for_pass_2(self):
        verdict = run(JUNK_TEXT, pass_no=1, signed=True, bert=bert(0.99))
        assert verdict.rule != "R3"

    def test_r4_distilbert_above_85_forces_junk(self):
        verdict = run(FILLER, bert=bert(0.90, "JUNK_LETTER_FAX"))
        assert (verdict.code, verdict.rule) == (classify.CODE_LETTER_FAX, "R4")
        assert verdict.confidence == pytest.approx(0.90)

    def test_r4_leaves_a_clinical_page_to_the_vote(self):
        text = "Assessment:\nstable\nPlan:\nrest " + FILLER  # clinical 4
        verdict = run(text, bert=bert(0.99))
        assert verdict.rule == "R9" and verdict.code == classify.CODE_MAIN
        assert verdict.votes["r4_skipped"] == "clinical"

    def test_r4_leaves_a_face_sheet_to_the_vote(self):
        text = "Patient Demographics face sheet guarantor emergency contact date of birth " + FILLER
        verdict = run(text, bert=bert(1.0))
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
        verdict = run(CLINICAL_TEXT + "\nChief Complaint:\nchest pain", bert=bert(0.7))
        assert (verdict.code, verdict.rule) == (classify.CODE_MAIN, "R7")

    def test_r8_visual_is_kept(self):
        verdict = run(FILLER, document_type="visual", bert=bert(0.8))
        assert (verdict.code, verdict.rule) == (classify.CODE_MAIN, "R8")

    def test_r8_visual_with_invoice_wording_is_junk(self):
        text = "Invoice number 12, amount due, remit to, balance due"  # < 20 words: Visual
        verdict = run(text, document_type="visual")
        assert (verdict.code, verdict.rule) == (classify.CODE_INVOICE, "R8")

    def test_r8_visual_needs_fewer_than_20_words(self):
        # 19 words: still Visual. 20 words: judged like any other page (the vote here).
        assert run(" ".join(["word"] * 19), document_type="visual", bert=bert(0.8)).rule == "R8"
        verdict = run(" ".join(["word"] * 20), document_type="visual", bert=bert(0.8))
        assert verdict.rule != "R8"
        assert "not Visual" in verdict.votes["visual"]
        assert method.text_is_visual("visual", "a b c") and not method.text_is_visual("printed", "a")


class TestVote:
    """Two voters, DistilBERT then keywords (method §8.2)."""

    def test_jj_with_a_strong_ballot_is_junk(self):
        verdict = run(JUNK_TEXT, bert=bert(0.84))  # DistilBERT weak, keywords strong (12)
        assert (verdict.votes["ballots"], verdict.votes["row"], verdict.code) == ("JJ", 1, classify.CODE_INVOICE)
        # both agree: 1 - (1 - 0.84) * (1 - 1.0)
        assert verdict.confidence == pytest.approx(1.0)

    def test_jj_both_weak_is_junk_too(self):
        verdict = run(FAX_TEXT, bert=bert(0.70))
        assert verdict.votes["ballots"] == "JJ" and verdict.code != classify.CODE_MAIN

    def test_jj_both_weak_waits_in_pass_1(self):
        verdict = run(FAX_TEXT, pass_no=1, bert=bert(0.70))
        assert verdict.code == classify.CODE_MAIN and "deferred" in verdict.reason

    def test_jj_still_yields_to_the_handwritten_veto(self):
        verdict = run(FAX_TEXT, bert=bert(0.70), hw_area_pct=45.0)
        assert (verdict.code, verdict.votes.get("veto")) == (classify.CODE_MAIN, "handwritten")

    def test_ja_distilbert_alone_is_junk(self):
        verdict = run(FILLER, bert=bert(0.70, "JUNK_LETTER_FAX"))
        assert (verdict.votes["ballots"], verdict.votes["row"], verdict.code) == ("JA", 2, classify.CODE_LETTER_FAX)
        assert verdict.confidence == pytest.approx(0.70)

    def test_ja_handwritten_veto_keeps_with_distilberts_low_keep(self):
        verdict = run(FILLER, bert=bert(0.70), hw_area_pct=45.0)
        assert (verdict.code, verdict.votes["veto"]) == (classify.CODE_MAIN, "handwritten")
        assert verdict.confidence == pytest.approx(0.30)

    def test_aj_at_the_threshold_is_junk(self):
        verdict = run(INVOICE_TEXT)  # no DistilBERT: abstains
        assert (verdict.votes["ballots"], verdict.votes["row"], verdict.code) == ("AJ", 3, classify.CODE_INVOICE)
        assert verdict.confidence == pytest.approx(0.90)

    def test_aj_below_the_threshold_stays_main(self):
        verdict = run(FAX_TEXT)
        assert (verdict.votes["ballots"], verdict.code) == ("AJ", classify.CODE_MAIN)
        assert verdict.confidence == pytest.approx(0.60)  # 1 - 4/10: the keywords lean junk

    def test_aj_under_8_words_counts_distilbert_as_abstaining(self):
        verdict = run("Invoice amount due, remit to, balance due", bert=bert(0.10))  # 7 words
        assert (verdict.votes["ballots"], verdict.code) == ("AJ", classify.CODE_INVOICE)
        assert "bert" not in verdict.votes["confidence_from"]

    def test_jk_disagreement_is_main_with_low_confidence(self):
        verdict = run(CLINICAL_WEAK, bert=bert(0.80))
        assert (verdict.votes["ballots"], verdict.votes["row"], verdict.code) == ("JK", 4, classify.CODE_MAIN)
        # disagree: mean(DistilBERT p(keep) 0.20, keywords 0.40)
        assert verdict.confidence == pytest.approx(0.30)
        assert verdict.votes["confidence_from"]["combined"] == "disagree"

    def test_kk_agreement_reinforces(self):
        verdict = run(CLINICAL_WEAK, bert=bert(0.02))
        assert (verdict.votes["row"], verdict.code) == (5, classify.CODE_MAIN)
        assert verdict.confidence == pytest.approx(1 - 0.02 * 0.6)

    def test_a_clear_keep_keeps_distilberts_confidence(self):
        verdict = run(FILLER, bert=bert(0.02))
        assert (verdict.votes["ballots"], verdict.code) == ("KA", classify.CODE_MAIN)
        assert verdict.confidence == pytest.approx(0.98)

    def test_nothing_to_judge_is_main(self):
        verdict = run(FILLER, bert=bert(0.55))  # DistilBERT in its abstain band
        assert (verdict.votes["ballots"], verdict.votes["row"], verdict.code) == ("AA", 7, classify.CODE_MAIN)
        assert verdict.confidence == pytest.approx(0.45)

    def test_a_missing_model_abstains_and_gives_no_signal(self):
        verdict = run(FILLER)
        assert verdict.votes["bert"]["note"] == "unavailable"
        assert verdict.confidence == pytest.approx(0.5)

    def test_ballot_letters_are_distilbert_then_keywords(self):
        verdict = run(FILLER, bert=bert(0.70))
        by = verdict.votes["by_voter"]
        assert set(by) == {"bert", "kw"}
        assert verdict.votes["ballots"] == by["bert"] + by["kw"]
        assert f"vote {verdict.votes['ballots']} (DistilBERT, keywords)" in verdict.reason

    def test_clinical_content_keeps_the_page_without_a_veto(self):
        # C >= 4: the keywords vote KEEP, so the vote is JK -> Main; no veto is needed.
        text = JUNK_TEXT + "\nAssessment:\nstable\nPlan:\nrest"
        verdict = run(text, bert=bert(0.84))
        assert (verdict.code, verdict.votes["ballots"], verdict.votes.get("veto")) == (classify.CODE_MAIN, "JK", None)

    def test_a_face_sheet_with_soft_junk_wording_is_kept_by_the_keywords(self):
        # D >= 3 with fax wording (not a hard group): the keywords vote KEEP.
        text = "Patient Name DOB MRN Sex face sheet guarantor. Fax cover sheet, number of pages 3. " + FILLER
        verdict = run(text, bert=bert(0.84))
        assert (verdict.code, verdict.votes["by_voter"]["kw"], verdict.votes.get("veto")) == (classify.CODE_MAIN, "K", None)

    def test_handwritten_veto_above_30_percent(self):
        verdict = run(JUNK_TEXT, bert=bert(0.84), hw_area_pct=45.0)
        assert (verdict.code, verdict.votes.get("veto")) == (classify.CODE_MAIN, "handwritten")

    def test_no_handwritten_veto_at_30_percent_or_less(self):
        verdict = run(JUNK_TEXT, bert=bert(0.84), hw_area_pct=30.0)
        assert verdict.code == classify.CODE_INVOICE

    def test_pass_1_only_takes_two_strong_junk_ballots(self):
        weak = run(JUNK_TEXT, pass_no=1, bert=bert(0.84))
        assert weak.code == classify.CODE_MAIN and "deferred" in weak.reason
        strong = run(JUNK_TEXT, pass_no=1, bert=bert(0.95))
        assert strong.code == classify.CODE_INVOICE

    def test_pass_1_defers_ja_and_aj(self):
        assert "deferred" in run(FILLER, pass_no=1, bert=bert(0.95)).reason
        assert "deferred" in run(INVOICE_TEXT, pass_no=1).reason

    def test_every_verdict_records_its_ballots(self):
        verdict = run(JUNK_TEXT, bert=bert(0.84))
        assert {"bert", "kw", "rule", "ballots", "row", "confidence_from"} <= set(verdict.votes)
        assert "tfidf" not in verdict.votes
        assert verdict.votes["kw"]["junk_group"] == "invoice"


class TestConfidence:
    def test_observation_rules_are_fixed(self):
        assert run("   ").confidence == 1.0
        assert run("This page intentionally left blank").confidence == 0.99

    def test_r7_combines_the_rule_with_a_disagreeing_distilbert(self):
        verdict = run(CLINICAL_TEXT + "\nChief Complaint:\nchest pain", bert=bert(0.7))
        assert verdict.rule == "R7"
        signals = verdict.votes["confidence_from"]
        assert signals["rule"] == 0.95 and signals["bert"] == pytest.approx(0.30)
        assert signals["combined"] == "disagree"
        assert verdict.confidence == pytest.approx((0.95 + 0.30 + signals["keywords"]) / 3, abs=1e-4)

    def test_r7_with_agreement_is_at_least_the_rule(self):
        verdict = run(CLINICAL_TEXT + "\nChief Complaint:\nchest pain", bert=bert(0.05))
        assert verdict.rule == "R7" and verdict.confidence >= 0.95


class TestPatientEducation:
    HANDOUT = (
        "Dim lighting Loose rug Clutter. This picture shows some of the things that can cause a fall in "
        "your home. Consumer Information Use and Disclaimer. This generalized information is a limited "
        "summary of diagnosis, treatment, and/or medication information."
    )

    def test_a_handout_is_clinical_so_distilbert_cannot_force_it(self):
        verdict = run(self.HANDOUT, bert=bert(0.99, "JUNK_RECORD_REQUEST"))
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


class TestPass2ReadsTesseractJunkTerms:
    """Chart 66945468 p25: 'Optum' was in the Tesseract text only; the final OCR dropped it."""

    FINAL = "Patient Name DOB MRN Sex " + " ".join(f"field{i}" for i in range(30))
    PRELIM = FINAL + " Optum"

    def test_a_junk_term_only_tesseract_read_counts_in_pass_2(self):
        scores = score_text(self.FINAL, also_junk=self.PRELIM)
        assert scores.junk == score_text(self.PRELIM).junk > 0
        assert scores.tesseract_only == {"junk.roi_vendors": ["optum"]}
        assert scores.to_dict()["tesseract_only"] == {"junk.roi_vendors": ["optum"]}

    def test_clinical_and_demographic_read_the_final_text_only(self):
        alone = score_text("Patient Name only")
        scores = score_text("Patient Name only", also_junk="Assessment: Plan: DOB MRN Sex")
        assert (scores.clinical, scores.demographic) == (alone.clinical, alone.demographic)

    def test_the_keyword_ballot_votes_junk_with_it(self):
        verdict = run(self.FINAL, prelim_text=self.PRELIM, bert=bert(0.71, "JUNK_OTHERS"))
        assert verdict.votes["kw"]["vote"] == "JUNK" and verdict.votes["ballots"] == "JJ"

    def test_pass_1_does_not_use_it(self):
        verdict = run(self.FINAL, pass_no=1, prelim_text="Optum")
        assert "tesseract_only" not in verdict.votes["kw"]


def test_r5_records_the_ink_it_measured():
    lines = [Line("Optum ROI request", 0.03, 0.05)]
    verdict = run("Optum ROI request for records", lines=lines, body_ink=True, body_ink_fraction=0.0042)
    assert verdict.votes["short_page"]["ink"] is True
    assert (verdict.votes["short_page"]["ink_fraction"], verdict.votes["short_page"]["ink_limit"]) == (0.0042, 0.005)


def test_r5_short_page_is_15_percent():
    # Content ending at 14% is short; at 18% it is not (20% between 2026-10-18 and -19).
    short = [Line("Optum release of information", 0.03, 0.05), Line("Patient DOB MRN", 0.12, 0.14)]
    assert run("Optum release of information Patient DOB MRN", lines=short, body_ink=False).rule == "R5"
    longer = [Line("Optum release of information", 0.03, 0.05), Line("Patient DOB MRN", 0.16, 0.18)]
    assert run("Optum release of information Patient DOB MRN", lines=longer, body_ink=False).rule != "R5"


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


def test_r5_is_undone_only_by_both_keeps():
    lines = [Line("Assessment:", 0.03, 0.05), Line("Plan: rest", 0.08, 0.10)]
    # DistilBERT KEEP and keywords KEEP (KK): the short page is undone.
    kept = run(CLINICAL_WEAK, lines=lines, body_ink=False, bert=bert(0.1))
    assert (kept.votes["bert"]["vote"], kept.votes["kw"]["vote"]) == ("KEEP", "KEEP")
    assert kept.rule != "R5" and kept.votes["short_page"]["skipped"] == "KK"
    # One KEEP only (DistilBERT JUNK, keywords KEEP): R5 stands.
    one = run(CLINICAL_WEAK, lines=lines, body_ink=False, bert=bert(0.7))
    assert one.votes["bert"]["vote"] == "JUNK" and one.rule == "R5"
