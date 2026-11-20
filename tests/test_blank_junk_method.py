"""Blank / junk method (modules/blank_junk/method.py): canon scoring, voters, ladder, vote."""
from __future__ import annotations

import pytest

import modules.blank_junk.stage  # noqa: F401  — puts the blank_junk lib on sys.path

import classify  # noqa: E402
import method  # noqa: E402
from method import Line, PageInputs, decide, score_text  # noqa: E402

FILLER = "the quick brown fox jumps over a lazy dog near the river bank today"
JUNK_TEXT = "Invoice number 4411 amount due now. Fax cover sheet, number of pages 3, intended recipient only. " + FILLER
FAX_TEXT = "Fax cover sheet, number of pages 3. " + FILLER  # keywords: weak JUNK, score 4
INVOICE_TEXT = "Invoice number 4411, amount due, remit to, balance due " + FILLER  # strong JUNK, score 9
CLINICAL_WEAK = "Assessment:\nstable\nPlan:\nrest " + FILLER  # keywords: weak KEEP, clinical 4
# 2026-11-20 (user): note sections count only as real headers (markdown / stage 6 headings).
CLINICAL_TEXT = "## HPI\nchest pain\n## Assessment\nangina\n## Plan\nstress test, metformin 500 mg daily\n" + FILLER


def bert(p_junk: float, label: str = "JUNK_INVOICE") -> dict:
    """Raw model numbers plus the service's decision (JUNK at p(junk) >= 0.70, as v3's 0.70 flag
    threshold; else KEEP)."""
    p_keep = round(1.0 - p_junk, 4)
    flag = "JUNK" if p_junk >= 0.70 else "KEEP"
    return {"p_keep": p_keep, "p_junk": p_junk, "p_blank": 0.0, "top_junk_label": label,
            "service": {"flag": flag, "subclass": label if flag == "JUNK" else "",
                        "confidence": p_junk if flag == "JUNK" else p_keep, "review_required": False,
                        "decision_reason": "argmax", "audit_tag": "", "p_keep": p_keep, "p_blank": 0.0,
                        "p_junk": p_junk}}


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
        # 2026-11-19 (user): pass 2 does not redo pass 1's text rules, except on a page pass 1
        # never judged (pass1_skipped).
        verdict = run("Intentionally left blank", pass_no=1)
        assert (verdict.code, verdict.rule) == (classify.CODE_BLANK, "R1")
        verdict = run("Intentionally left blank", pass1_skipped=True)
        assert (verdict.code, verdict.rule) == (classify.CODE_BLANK, "R1")

    def test_r1_under_15_characters_is_junk_others(self):
        assert run("Page 3 of 7").rule != "R1"  # pass 2: pass 1 already judged it
        verdict = run("Page 3 of 7", pass1_skipped=True)
        assert (verdict.code, verdict.rule) == (classify.CODE_OTHERS, "R1")



    def test_r6_does_not_fire_with_ink_below_or_unmeasured(self):
        lines = [Line("Wound care photo", 0.03, 0.05)]
        assert run("Wound care photo and notes", lines=lines, body_ink=True).rule != "R6"
        assert run("Wound care photo and notes", lines=lines, body_ink=None).rule != "R6"

    def test_r6_content_past_15_percent_is_not_short(self):
        lines = [Line("header", 0.03, 0.05), Line("more text", 0.20, 0.22)]
        assert run("header more text here", lines=lines, body_ink=False).rule != "R6"

    def test_r4_signature_beats_r5(self):
        assert run(FILLER, signed=True, signature_known=True, bert=bert(0.9)).rule == "R4"





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
    from common.db.memory_store import MemoryStore

    store = MemoryStore()
    for page_id, kind, hw in ((1, "uncertain", "blank_page"), (2, "uncertain", "faint_marks_only"),
                              (3, "handwritten", "convnext_page_tags")):
        store.upsert_quality(chart_id=7, page_id=page_id, printed_or_handwritten=kind, hw_method=hw)
    assert store.non_printed_page_ids(7) == {3}
    verdict = run("", pass_no=1, no_ink=True)
    assert (verdict.code, verdict.rule) == (classify.CODE_BLANK, "R1")


def test_a_page_typed_blank_is_judged_in_pass_1_and_is_blank_without_text():
    # 2026-10-27 (user): Type (Printed/HW) = Blank is no longer "non-printed": pass 1 reads it,
    # and with no text R1 calls it Blank (before, it reached pass 2 with no text and got no verdict).
    from common.db.memory_store import MemoryStore

    store = MemoryStore()
    store.upsert_quality(chart_id=8, page_id=1, printed_or_handwritten="uncertain", document_type="blank",
                         hw_method="convnext_page_tags")
    assert store.non_printed_page_ids(8) == set()
    verdict = run("", pass_no=1)
    assert (verdict.code, verdict.rule) == (classify.CODE_BLANK, "R1")


CARD_TEXT = (
    "Benefit Award Card # 8101 3360 3004 6207 Name: Jane Q Sample For Pharmacist: 1-877-889-6510 "
    "Rx Claims: OptumRx UnitedHealthcare UCard Member ID 903710875-00 "
    "RxBIN 610097 RxPCN 9999 RxGRP COS Optum Health Networks"
)




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


class TestCardOnly:
    """2026-11-01 (user): card wording alone (the old Visual R3) junks nothing."""

    def test_card_words_without_a_strong_card_go_on_to_distilbert(self):
        for text in ("Member card", "Aetna Datavant member card", "RxBIN 610097 RxPCN 9999 RxGRP COS"):
            verdict = run(text, document_type="visual", bert=bert(0.1))
            assert verdict.rule != "R3" and verdict.code == classify.CODE_MAIN, text

    def test_a_handwritten_page_is_never_read_for_a_card(self):
        assert run(CARD_TEXT, document_type="handwritten", bert=bert(0.0)).rule != "R3"


class TestDistilBertLadder:
    """2026-10-20: R1-R4, then DistilBERT alone; junk only in pass 2 except R1-R3."""

    def test_r2_placeholder_is_blank_not_junk(self):
        verdict = run("This page intentionally left blank", bert=bert(0.99), pass1_skipped=True)
        assert (verdict.code, verdict.rule) == (classify.CODE_BLANK, "R2")





    def test_pass_2_junk_cut_offs(self):
        sure = run(FILLER, bert=bert(0.90, "JUNK_LETTER_FAX"))
        assert (sure.code, sure.rule) == (classify.CODE_LETTER_FAX, "R5") and "review" not in sure.reason
        # 2026-11-03 (user): p(junk) >= 0.70 is junk, below is Main; no review band.
        at = run(FILLER, bert=bert(0.70))
        # the model's Invoice is Main / Invoice page (2026-11-14: no amount / date check).
        assert at.code == method.CODE_MAIN and method.is_invoice_page(at.reason) and not at.votes.get("review_required")
        assert run(FILLER, bert=bert(0.69)).code == classify.CODE_MAIN

    def test_keywords_no_longer_decide(self):
        # Invoice wording, DistilBERT keeps: Main. Clinical wording, DistilBERT junks: Junk.
        assert run(INVOICE_TEXT, bert=bert(0.10)).code == classify.CODE_MAIN
        assert run(FILLER, bert=bert(0.95, "JUNK_LETTER_FAX")).code != classify.CODE_MAIN
        # 2026-11-19 (user): 2+ Progress Note sections keep the page, whatever the model says.
        note = run(CLINICAL_TEXT, bert=bert(0.95, "JUNK_LETTER_FAX"))
        assert (note.code, note.rule) == (classify.CODE_MAIN, "CLINICAL_CONTENT")

    def test_a_signature_keeps_the_page_unless_distilbert_is_over_95(self):
        held = run(FILLER, signed=True, bert=bert(0.94))
        assert (held.code, held.rule) == (classify.CODE_MAIN, "R4")
        over = run(FILLER, signed=True, bert=bert(0.97, "JUNK_LETTER_FAX"))
        assert over.rule == "R5" and over.code != classify.CODE_MAIN and "r4_overruled" in over.votes


    def test_no_model_keeps_the_page_for_review(self):
        verdict = run(FILLER)
        assert verdict.code == classify.CODE_MAIN and verdict.votes["review_required"]




class TestPostProcess:
    """2026-10-26 (user): a page with a signature or provider details is never junk; a DOS
    alone does not keep it (removed the same day)."""

    def test_plain_junk_and_pass_1_are_unchanged(self):
        # JUNK_OTHERS: a Junk / Invoice page is kept by the INVOICE rule (2026-11-05).
        for inputs in (PageInputs(text=JUNK_TEXT, pass_no=2, bert=bert(0.99, "JUNK_OTHERS")),
                       PageInputs(text=JUNK_TEXT, pass_no=1, bert=bert(0.99, "JUNK_OTHERS"), signed=True)):
            verdict = decide(inputs)
            assert method.post_process(verdict, inputs) is verdict





def svc(flag: str, confidence: float, subclass: str = "", review: bool = False, reason: str = "argmax") -> dict:
    p_keep = confidence if flag == "KEEP" else 1.0 - confidence
    p_junk = confidence if flag == "JUNK" else 0.0
    return {"p_keep": p_keep, "p_junk": 1.0 - p_keep, "top_junk_label": subclass,
            "service": {"flag": flag, "subclass": subclass, "confidence": confidence, "review_required": review,
                        "decision_reason": reason, "audit_tag": "", "p_keep": p_keep,
                        "p_blank": 0.0, "p_junk": p_junk}}




class TestBlankFromStage1AndLabKeep:
    """2026-11-04 (user)."""

    def test_a_page_stage_1_calls_blank_is_blank_not_junk(self):
        for pass_no in (1, 2):
            v = run("x .", pass_no=pass_no, document_type="blank", bert=bert(0.99))
            assert (v.code, v.rule) == (method.CODE_BLANK, "R1")

    def test_stage_1_blank_with_text_on_the_page_is_not_blank(self):
        """2026-11-15 (user): a page typed blank with lines of text on it is not Blank."""
        text = "Outside Labs: Ameripath Quest. In-House Tests OK? YES - AON Lab / YES - AON Path"
        v = run(text, pass_no=1, document_type="blank")
        assert v.code != method.CODE_BLANK and v.votes.get("stage1_blank_overruled")

    def test_stage_1_blank_never_blanks_a_page_with_text(self):
        """2026-11-20 (user): stage 1's Blank on a page with text is not Blank (the header-only
        Blank and the stage-1-Blank fallback are gone): the other rules and the model decide."""
        header = "Patient Name: X Date of Birth: 01/31/1944 Sex: F Encounter Date: April 27, 2026"
        v = run(header, pass_no=2, document_type="blank", content_bottom=0.03, body_ink=False, bert=bert(0.1))
        assert v.code == method.CODE_MAIN and v.votes.get("stage1_blank_overruled")
        text = "Outside Labs: Ameripath Quest. In-House Tests OK? YES - AON Lab / YES - AON Path"
        assert run(text, pass_no=2, document_type="blank", bert=bert(0.1)).code == method.CODE_MAIN
        assert run(text, pass_no=2, document_type="blank", signed=True, bert=bert(0.1)).code == method.CODE_MAIN
        # No text at all: still Blank.
        assert run("", pass_no=2, document_type="blank").code == method.CODE_BLANK


    def test_all_three_words_are_needed(self):
        no_range = "Lab report. Test Value Units. Invoice number 4411 amount due now. " + FILLER
        v = method.post_process(run(no_range, bert=bert(0.99, "JUNK_OTHERS")), PageInputs(text=no_range, pass_no=2))
        assert v.code != method.CODE_MAIN


def test_p_blank_is_never_part_of_p_junk():
    """2026-11-04 (user)."""
    labels = ["KEEP", "BLANK", "JUNK_INVOICE", "JUNK_OTHERS"]
    p_keep, p_junk, top = method.bucket(labels, [0.10, 0.60, 0.20, 0.10])
    assert (round(p_keep, 4), round(p_junk, 4), top) == (0.10, 0.30, "JUNK_INVOICE")
    assert round(method.blank_probability(labels, [0.10, 0.60, 0.20, 0.10]), 4) == 0.60


def test_lab_results_overrule_the_admin_rule():
    """2026-11-04 (user): a lab table is not an index (v3 admin rule: contents_page)."""
    import model_bridge

    assert model_bridge.lab_result_signs("Component Value Unit Reference Range\nOxygen Saturation 90- 100%")
    assert model_bridge.lab_result_signs("Oxygen Saturation 90- 100%")
    assert model_bridge.lab_result_signs("Glucose 98 mg/dL")
    assert not model_bridge.lab_result_signs("CONTENTS\nOffice Visit 01/02/2024\nRadiology pages 3 - 9")


PROBLEM_HEAD = ("Patient Name: Shaw, Amanda Date of Birth: 08/02/1945 Sex:F Encounter Date: January 22, 2026\n"
                "Name Status Onset Date Source Resolution Date Notes\n")
PROBLEMS = ["Diabetic Peripheral Neuropathy", "Thrombocytopenic Disorder", "Neutropenia", "Cirrhosis of Liver",
            "Anemia", "Insomnia", "Bronchitis", "Subacute Cough"]


class TestProblemListIsNotAnIndex:
    """2026-11-06 (user): the v3 admin rule junked a problem list ("Anemia  Active  11/06/2025")
    as a contents page. Dates alone no longer make an index, and the bridge overrules the admin
    rule on a clinical list."""

    def _vendored(self, name):
        import sys

        import model_bridge

        vendor = str(model_bridge._VENDOR)
        sys.path.insert(0, vendor)
        try:
            from src.preprocessing import page_subclass
        finally:
            sys.path.remove(vendor)
        return getattr(page_subclass, name)

    def _admin(self, text):
        return self._vendored("admin_page_reason")(text)

    def test_a_problem_list_is_not_a_contents_page(self):
        text = PROBLEM_HEAD + "".join(f"{name} Active 11/06/2025\n" for name in PROBLEMS)
        assert self._admin(text) is None

    def test_a_real_index_still_is(self):
        assert self._admin("Progress Notes 1 - 12\nLab Results 13 - 20\nRadiology 21 to 25\n")[1] == "contents_page"
        contents = self._vendored("contents_page_reason")
        dated = "Office Visit 01/02/2024\nLab Report 01/05/2024\nRadiology 02/01/2024\n"
        assert contents("CONTENTS\n" + dated) == "contents_page"
        assert contents(dated) is None  # dates alone, no title

    def test_the_bridge_sees_a_clinical_list(self):
        import model_bridge

        short = PROBLEM_HEAD + "".join(f"{name} Active 11/06/2025\n" for name in PROBLEMS[:4])
        assert model_bridge.clinical_list_signs(short)
        assert model_bridge.clinical_list_signs("Problem List\nAnemia 11/06/2025")
        assert model_bridge.clinical_list_signs("Dementia 08/30/2023 Problem Code: F03.90")
        assert not model_bridge.clinical_list_signs("CONTENTS\nOffice Visit 01/02/2024\nRadiology pages 3 - 9")


class TestSimplifiedLadder:
    """2026-11-04 (user): the simplified blank / junk ladder."""

    def test_pass_1_is_rules_only(self):
        assert run("", pass_no=1).code == method.CODE_BLANK
        assert run("This page intentionally left blank", pass_no=1).code == method.CODE_BLANK
        assert run("ACCEPT", pass_no=1).code == classify.CODE_COVER_PAGE
        assert run("UNACCEPT  COVER", pass_no=1).code == classify.CODE_COVER_PAGE
        assert run("x.7 :", pass_no=1).code == classify.CODE_OTHERS  # under 15 characters
        v = run(JUNK_TEXT, pass_no=1, bert=bert(0.99))  # the model is not read in pass 1
        assert (v.code, v.rule) == (method.CODE_MAIN, "P1")
        # 2026-11-08: a cover mark (or End of Report) with fewer than 10 words on the page; 10 or
        # more is not (was 5).
        assert run("Accept this invoice please", pass_no=1).code == classify.CODE_COVER_PAGE
        assert run("Fax cover 2 pages", pass_no=1).code == classify.CODE_COVER_PAGE
        assert run("Accept this invoice please today, thank you very much", pass_no=1).code == classify.CODE_COVER_PAGE
        assert run("Accept this invoice please today, thank you very much indeed", pass_no=1).rule == "P1"
        v = run("*** End of Report *** printed 01/02/2024 by lab", pass_no=1)
        assert (v.code, v.rule) == (classify.CODE_COVER_PAGE, "R1E")
        assert run("End of Report for the patient seen in clinic today with notes", pass_no=1).rule == "P1"

    def test_a_card_comes_before_the_visual_keep(self):
        v = run(CARD_TEXT, document_type="visual", bert=bert(0.5))
        assert v.code == classify.CODE_CARDS
        assert run(" ".join(["word"] * 19), document_type="visual", bert=bert(0.99)).rule == "R3V"

    def test_handwritten_over_30_percent_is_main(self):
        v = run(JUNK_TEXT, bert=bert(0.99), hw_area_pct=45.0)
        assert (v.code, v.rule) == (method.CODE_MAIN, "HW")

    def test_signed_keeps_unless_raw_p_junk_over_95(self):
        """2026-11-07 (user): only kv_extract's Electronic Signature keeps a page; signature
        words in the text do not."""
        assert run(FILLER, bert=bert(0.80), signed=True).rule == "R4"
        assert run(FILLER, bert=bert(0.99, "JUNK_LETTER_FAX"), signed=True).code != method.CODE_MAIN
        assert run("Electronically signed by Dr X. " + FILLER, bert=bert(0.80, "JUNK_LETTER_FAX")).rule == "R5"
        assert run("Signature: ____ " + FILLER, bert=bert(0.80, "JUNK_LETTER_FAX")).rule == "R5"

    def test_the_service_decides_junk_unless_raw_p_keep_is_99(self):
        assert run(FILLER, bert=bert(0.80, "JUNK_LETTER_FAX")).code == classify.CODE_LETTER_FAX
        b = bert(0.80)
        b["p_keep"] = 0.995  # the service says JUNK (an admin rule) but the model is sure of a keep
        assert run(FILLER, bert=b).code == method.CODE_MAIN
        b = bert(0.0)
        b["service"]["flag"] = "BLANK"
        # 2026-11-05 (user): the classifier's Blank is Junk / Others.
        assert run(FILLER, bert=b).code == classify.CODE_OTHERS
        assert run(FILLER, bert=bert(0.10)).code == method.CODE_MAIN

    def test_no_model_is_main_for_review(self):
        v = run(FILLER, bert=None)
        assert v.code == method.CODE_MAIN and v.votes.get("review_required")

    def test_provider_details_no_longer_keep_a_page(self):
        v = method.post_process(run(JUNK_TEXT, bert=bert(0.99, "JUNK_OTHERS"), provider=True),
                                PageInputs(text=JUNK_TEXT, pass_no=2, provider=True))
        assert v.code != method.CODE_MAIN

    def test_a_lab_result_is_main_in_pass_2(self):
        lab = ("Quest Lab report. Test Value Reference Range Units. Glucose 98 70-99 mg/dL. "
               "Invoice number 4411 amount due now. " + FILLER)
        v = method.post_process(run(lab, bert=bert(0.99)), PageInputs(text=lab, pass_no=2))
        assert (v.code, v.rule) == (method.CODE_MAIN, "LAB")


class TestInvoiceKept:
    """2026-11-05 (user): a Junk / Invoice page is Main (Blank/Junk No), page type Invoice /
    Invoice Page."""

    def test_a_junk_invoice_page_is_main_by_the_invoice_rule(self):
        """Any Junk / Invoice page (2026-11-14, user: the amount + Invoice / Due Date check is gone)."""
        for text in (INVOICE_TEXT, INVOICE_TEXT + " Statement date 03/10/2026, total $120.00"):
            inputs = PageInputs(text=text, pass_no=2, bert=bert(0.99))
            v = method.post_process(decide(inputs), inputs)
            assert (v.code, v.rule) == (method.CODE_MAIN, "INVOICE")
            assert method.is_invoice_page(v.reason) and v.confidence >= 0.9

    def test_a_terms_page_is_junk_others(self):
        """2026-11-14 (user): T&C, Terms and Conditions, Payment Terms, Payment Instructions ...
        BEFORE the invoice wording -> Junk / Others."""
        for words in ("T&C apply", "T & C", "Terms and Conditions", "TERMS & CONDITIONS", "Payment Terms: Net 30",
                      "Terms of Payment", "Terms of Sale", "General Terms", "Standard terms",
                      "Payment Instructions", "Terms &amp; Conditions"):
            inputs = PageInputs(text=words + " " + INVOICE_TEXT, pass_no=2, bert=bert(0.99))
            v = method.post_process(decide(inputs), inputs)
            assert v.code == classify.CODE_OTHERS and v.votes.get("invoice_not_kept"), words
        # found in the Tesseract text too; a page with no invoice wording at all.
        inputs = PageInputs(text=FILLER, prelim_text="terms and conditions", pass_no=2, bert=bert(0.99))
        assert decide(inputs).code == classify.CODE_OTHERS
        # "terms" alone is not a terms page.
        inputs = PageInputs(text="short term care " + INVOICE_TEXT, pass_no=2, bert=bert(0.99))
        assert decide(inputs).code == method.CODE_MAIN

    def test_terms_after_the_invoice_is_an_invoice(self):
        """2026-11-14 (user): "Payment Instructions: Please refer to the Payment Terms & Conditions
        on next page" below the invoice -> still an invoice."""
        text = (INVOICE_TEXT + " Payment Instructions: Please refer to the Important Payment "
                "Terms &amp; Conditions on next page.")
        inputs = PageInputs(text=text, pass_no=2, bert=bert(0.99))
        v = method.post_process(decide(inputs), inputs)
        assert (v.code, v.rule) == (method.CODE_MAIN, "INVOICE")

    def test_a_dollar_table_is_an_invoice_whatever_the_terms(self):
        """2026-11-14 (user): a table with a dollar amount in Final OCR 1 (Docling) is an invoice;
        terms / payment instructions can't overrule it."""
        table = "| ITEM | TOTAL |\n| Chart Fee | $25.00 |\n| TOTAL | $25.00 |"
        text = "Payment Instructions: Terms and Conditions apply. " + FILLER
        inputs = PageInputs(text=text, final1_text=table, pass_no=2, bert=bert(0.99))
        v = method.post_process(decide(inputs), inputs)
        assert (v.code, v.rule) == (method.CODE_MAIN, "INVOICE") and v.votes.get("invoice_table") == "$25.00"
        # An HTML table cell (Doc Intel) counts too; a dollar amount outside a table does not.
        assert method.invoice_table(PageInputs(text="<table><tr><td>$1,200.50</td></tr></table>", pass_no=2))
        assert method.invoice_table(PageInputs(text="copay $25 today", pass_no=2)) is None

    def test_a_care_providers_page_without_invoice_wording_is_unjunked(self):
        """2026-11-15 (user): the model's Invoice with no invoice wording but Surgery Center /
        Hospital / Clinic on it -> Main, page classification decides (not Invoice page)."""
        letter = "Saint Clare Surgery Center. Explanation of our billing process. We bill your insurance. " + FILLER
        inputs = PageInputs(text=letter, pass_no=2, bert=bert(0.99))
        v = method.post_process(decide(inputs), inputs)
        assert v.code == method.CODE_MAIN and not method.is_invoice_page(v.reason)
        assert "Surgery Center" in v.votes.get("invoice_unjunked", "")
        # invoice wording wins over the facility name.
        inputs = PageInputs(text="Mercy Hospital " + INVOICE_TEXT, pass_no=2, bert=bert(0.99))
        assert method.is_invoice_page(method.post_process(decide(inputs), inputs).reason)
        # terms with no invoice wording stay Junk / Others.
        inputs = PageInputs(text="Terms and Conditions. Mercy Hospital " + FILLER, pass_no=2, bert=bert(0.99))
        assert decide(inputs).code == classify.CODE_OTHERS

    def test_a_card_the_model_calls_invoice_is_junk_cards(self):
        """2026-11-07 (user): an insurance card (card rule near miss, or the model's insurance-ID
        label) is Junk / Cards, never Invoice, and the invoice keep does not touch it."""
        card_text = ("UnitedHealthcare Member ID: 912345678 Payer ID 87726 PCP copay $25 "
                     "Group 70123 Rx Bin 610279 " + FILLER)
        inputs = PageInputs(text=card_text + " 03/10/2026", pass_no=2, bert=bert(0.97), document_type="printed")
        v = method.post_process(decide(inputs), inputs)
        assert v.code == classify.CODE_CARDS, v.reason
        b = bert(0.99, "JUNK_INSURANCE_ID")
        assert decide(PageInputs(text=FILLER, pass_no=2, bert=b)).code == classify.CODE_CARDS

    def test_other_junk_stays_junk(self):
        inputs = PageInputs(text=INVOICE_TEXT, pass_no=2, bert=bert(0.99, "JUNK_RECORD_REQUEST"))
        v = method.post_process(decide(inputs), inputs)
        assert v.code != method.CODE_MAIN and not method.is_invoice_page(v.reason)

    def test_page_classify_writes_invoice_page(self):
        from modules.page_classify import stage as pc
        row = pc._invoice_row(0.97)
        assert (row["page_type"], row["page_subtype"], row["decided_by"]) == ("Invoice", "Invoice Page", "invoice_page")
        assert row["classification_category"] == "non_codeable"


class TestCardKeeps:
    """2026-11-05 (user): a card goes through the same keeps as other junk."""

    def test_handwritten_signed_or_sure_keep_is_main(self):
        assert run(CARD_TEXT, document_type="printed", bert=bert(0.5), hw_area_pct=45.0).rule == "HW"
        assert run(CARD_TEXT, document_type="printed", bert=bert(0.5), signed=True).rule == "R4"
        v = run(CARD_TEXT, document_type="printed", bert=bert(0.005))
        assert (v.code, v.rule) == (method.CODE_MAIN, "R3")

    def test_otherwise_a_card_is_junk_cards(self):
        v = run(CARD_TEXT, document_type="printed", bert=bert(0.5))
        assert (v.code, v.rule) == (classify.CODE_CARDS, "R3")
        assert run(CARD_TEXT, document_type="printed", bert=None).code == classify.CODE_CARDS



class TestClinicalContentKeep:
    """2026-11-06 (user): a junk page with 2+ ICD-10 codes or a Social History header is Main."""

    def _keep(self, text, **kw):
        inputs = PageInputs(text=text, pass_no=2, bert=bert(0.99, "JUNK_OTHERS"), **kw)
        return method.post_process(decide(inputs), inputs)

    def test_icd_codes_keep_a_junk_page(self):
        v = self._keep(JUNK_TEXT + "\nI11.0 : Hypertensive heart disease\nE11.9 : Type 2 diabetes mellitus\n")
        assert (v.code, v.rule) == (method.CODE_MAIN, "CLINICAL_CONTENT") and "2 ICD-10 codes" in v.reason

    def test_one_code_is_not_enough(self):
        assert self._keep(JUNK_TEXT + "\nI11.0 : Hypertensive heart disease\n").code != method.CODE_MAIN

    def test_social_history_header_keeps_a_junk_page(self):
        assert self._keep(JUNK_TEXT, headings=("Social History",)).rule == "CLINICAL_CONTENT"
        assert self._keep(JUNK_TEXT + "\nSocial History\nTobacco Smoking Status Never Smoker\n").rule == "CLINICAL_CONTENT"

    def test_blank_stays_blank(self):
        inputs = PageInputs(text="", pass_no=2, headings=("Social History",))
        assert method.post_process(decide(inputs), inputs).code == method.CODE_BLANK


UHC_CARD = """UnitedHealthcare UCard
Hicks, Jonathan
Member ID 968980627-00
AARP Medicare Advantage Extra ValueRx UT-7 (HMO-POS)
Group Number: 42030 H4604-018-000 Payer ID: LIFE1
RxBIN RxPCN RxGRP 610097 9999 COS
PCP $0 Specialist $50
Benefit Award Card #: 6102 3300 3318 7266
For Members: myAARPMedicare.com 1-844-876-6176, TTY 711
Providers: optumproportal.com 1-877-370-2845
Med Claims: P.O Box 30539, Salt Lake City, UT 84130-0539
Rx Claims: OptumRx P.O. Box 650287, Dallas, TX 75265-0287"""


def test_a_card_needs_no_name():
    """2026-11-10 (user): the back of a card has no name, so no name is needed. Section headers
    do not block a card."""
    from card_rules import detect

    r = detect(UHC_CARD, headings=("For Members", "Providers:", "Rx Claims"), text_area=0.3)
    assert r.is_card and r.card_type == "insurance", r.blocked_by
    assert detect(UHC_CARD, headings=("Demographics",), text_area=0.3).is_card
    inputs = PageInputs(text=UHC_CARD, pass_no=2, document_type="printed", text_area=0.3,
                        bert=bert(0.5))
    assert decide(inputs).code == classify.CODE_CARDS


class TestVerdictConfidence2026_11_14:
    """CONFIDENCE_LOGIC.md section 12: the model's raw p for the verdict's class; a rule or veto
    against the model's top class -> flipped (0.30); no model -> backup (0.40); measured facts
    1.0; pass-1 text-pattern rules 0.95. The older value stays as ``strength``."""

    def test_measured_facts_and_pass1_rules(self):
        assert run("", pass_no=1, no_ink=True).confidence == 1.0
        assert run("", pass_no=1).confidence == 1.0  # no text
        assert run("blank", pass_no=1).confidence == 0.95
        tiny = run("abc 12", pass_no=1)
        assert (tiny.code, tiny.confidence, tiny.strength) == (classify.CODE_OTHERS, 0.95, 1.0)
        deferred = run(FILLER, pass_no=1)
        assert (deferred.code, deferred.rule, deferred.confidence) == (method.CODE_MAIN, "P1", 0.4)

    def test_the_service_verdict_is_the_models_own_probability(self):
        keep = run(FILLER, bert=bert(0.2))
        assert (keep.code, keep.rule, keep.confidence) == (method.CODE_MAIN, "R5", 0.8)
        junk = run(FAX_TEXT, bert=bert(0.9, "JUNK_LETTER_FAX"))
        assert junk.code == classify.CODE_LETTER_FAX and junk.confidence == 0.9

    def test_a_keep_against_the_models_top_class_reads_the_models_own_p(self):
        # 2026-11-19 (user, "make it real"; was flipped 0.30): the model's raw p for the verdict.
        vetoed = bert(0.6)
        vetoed["service"] = {**vetoed["service"], "flag": "KEEP", "confidence": 0.4}
        v = run(FILLER, bert=vetoed)
        assert (v.code, v.confidence) == (method.CODE_MAIN, 0.4)
        # Signed (R4) while p(junk) is the top class: a rule flipping the model, 0.40 (2026-11-19, user).
        signed = run(FILLER, bert=bert(0.8), signed=True, signature_known=True)
        assert (signed.rule, signed.confidence) == ("R4", 0.4) and signed.votes.get("flipped_model")
        assert signed.strength is not None and signed.strength != signed.confidence

    def test_no_model_is_a_backup(self):
        assert method.verdict_confidence(method.CODE_MAIN, None, None) == 0.4
        assert run(CLINICAL_TEXT).confidence == 0.4  # the classifier unavailable

    def test_an_invoice_kept_main_is_the_models_junk_call(self):
        inputs = PageInputs(text=INVOICE_TEXT, pass_no=2, bert=bert(0.97))
        v = method.post_process(decide(inputs), inputs)
        assert (v.code, v.rule, v.confidence) == (method.CODE_MAIN, "INVOICE", 0.97)
        terms = "Terms and Conditions apply. " + INVOICE_TEXT
        inputs = PageInputs(text=terms, pass_no=2, bert=bert(0.97))
        moved = method.post_process(decide(inputs), inputs)
        assert (moved.code, moved.confidence) == (classify.CODE_OTHERS, 0.3)



def test_a_contents_page_of_encounter_dates_is_junk():
    """2026-11-15 (user): CONTENTS with Encounter: <date> lines (no page numbers)."""
    text = ("NextGen Patient Record\nName: Davis Anthony\nCONTENTS\nEncounter: 4/14/2026 9:15:00 AM\nTRI_Full Exam\n"
            "Encounter: 2/25/2026 9:15:00 AM\nTRI_Office Procedure\nEncounter: 1/21/2026 9:30:00 AM\nTRI_Full Exam\n")
    assert method.index_page(text) is not None
    assert method.index_entries("Encounter: 2/11/2025 10:00:00 AM\nTRI_Laser\nEncounter: 2/4/2025 9:00:00 AM") == 2


def test_a_fax_cover_sheet_is_junk_even_when_signed():
    """2026-11-15 (user): Fax + its routing boxes (Urgent, For Your Review, Reply ASAP ...)."""
    text = ("Fax To: NW Womens Imaging From: Julie Laurie Bergstrom MD Remarks: Urgent For Your Review "
            "Reply ASAP Please Comment please send all previous DXA scan results on patient " + FILLER)
    v = run(text, signed=True, bert=bert(0.1))
    assert v.code == method.CODE_LETTER_FAX and v.votes.get("fax_cover")
    # a faxed clinical note (Fax header only) is not a cover sheet.
    assert method.fax_cover_sheet("Fax 555-1212 Progress Note Assessment Plan") is None


def test_a_hospital_page_without_the_word_card_is_not_a_card():
    """2026-11-15 (user): an Island Hospital Patient Abstract (Account Number, copay) the model calls
    an invoice and the card rule nearly matched -> not Cards; no invoice wording -> Main."""
    from types import SimpleNamespace
    text = ("Island Hospital PATIENT ABSTRACT Patient Rice Timothy Account Number IH59862714 Facility Island "
            "Hospital Tot Copay 18.98 Tot Est Reimb 93.33 " + FILLER)
    import modules.blank_junk.method as m
    orig = m.detect_card
    m.detect_card = lambda *a, **k: SimpleNamespace(card_type="insurance", matched=[], blocked_by="no card word")
    try:
        v = run(text, document_type="printed", bert=bert(0.97))
    finally:
        m.detect_card = orig
    assert v.code == method.CODE_MAIN and v.code != classify.CODE_CARDS


def test_a_medication_list_with_doses_is_main():
    """2026-11-15 (user): a medication list with doses (mg ...) is Main, like a lab result."""
    meds = ("Medications Reviewed Medications ezetimibe 10 mg tablet TAKE ONE TABLET BY MOUTH ONCE A DAY "
            "indapamide 1.25 mg tablet valsartan 80 mg tablet " + FILLER)
    v = run(meds, bert=bert(0.99, "JUNK_OTHERS"))
    assert (v.code, v.rule) == (method.CODE_MAIN, "MEDS")
    assert method.medication_doses("Rx: Prolia 60 mg/ml, Vitamin D 1.25 mg") is not None   # word + 2
    assert method.medication_doses("Tot Copay 18.98 Page 2 of 5") is None


def test_a_list_of_requested_record_types_is_not_a_note():
    """2026-11-20 (user): a records-request letter's table / bulleted list of record types
    ("Progress notes", "History and physical", "Problem list") is no Progress Note: the note
    sections keep does not fire; a real note's headers, each with its own text, still count."""
    from modules.blank_junk import pn_headers

    letter = ("Dear Provider Group,\nThis letter is to inform you of our chart review.\nFor Physicians\n"
              "\u2022 Progress notes for face-to-face office visit\n\u2022 History and physical\n"
              "\u2022 Problem list\n\u2022 Lab and screening results\nFor Hospitals\nProgress notes\n"
              "Consultation reports\nRadiology reports\nDischarge summary\n"
              "| Problem list | Discharge summary |")
    assert pn_headers.clinical_sections_on(letter) == []
    v = run(letter + " " + FILLER, bert=bert(0.9, "JUNK_RECORD_REQUEST"))
    assert v.rule != method.CLINICAL_CONTENT_RULE
    note = "## HPI\n72 year old presents with cough.\n## Assessment\nBronchitis\n## Plan\nRest and fluids."
    assert len(pn_headers.clinical_sections_on(note)) >= 2
    # 2026-11-20 (user): a section word only in the text, not a header, does not count.
    plain = "HPI:\n72 year old presents with cough.\nAssessment:\nBronchitis\nPlan:\nRest and fluids."
    assert pn_headers.clinical_sections_on(plain) == []
    assert len(pn_headers.clinical_sections_on(plain, ("HPI", "Assessment", "Plan"))) >= 2
