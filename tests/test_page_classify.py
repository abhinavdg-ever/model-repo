"""Page classification: taxonomy, keyword model, arbitration ladder, continuation rules.

Made-up text only.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CORE = REPO / "core-pipeline"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from stages.lib.page_classify import keywords, taxonomy  # noqa: E402
from stages.lib.page_classify.arbitration import (  # noqa: E402
    FinalPage,
    classify_page,
    decide_final,
    thresholds,
)
from stages.lib.page_classify.bert import BertResult  # noqa: E402
from stages.lib.page_classify.keywords import KeywordResult  # noqa: E402

PN = "Progress Note"


def bert(model_type, confidence, lead=0.0, top=()):
    return BertResult(model_type, taxonomy.load().page_type_of_model(model_type), confidence, lead, tuple(top))


def kw(page_subtype, score=5.0, margin=4.0, title_hit=True, top=()):
    names = taxonomy.load()
    page_type = names.page_type_of_subtype(page_subtype)
    return KeywordResult(page_subtype, page_type, names.model_type(page_type, page_subtype),
                         score, margin, title_hit, tuple(top))


ALL = frozenset(taxonomy.load().model_types)


# --- taxonomy ------------------------------------------------------------------


class TestTaxonomy:
    def test_counts_and_rules(self):
        names = taxonomy.load()
        assert len(names.page_types) == 49
        assert len(names.subtype_page_type) == 190
        assert len(names.model_types) == 58
        for page_type in names.page_types:  # every page type has its generic sub-type
            assert names.page_type_of_subtype(page_type) == page_type

    def test_model_type_rule(self):
        assert taxonomy.Taxonomy.model_type(PN, "Office Visit") == "Office Visit"
        assert taxonomy.Taxonomy.model_type("Laboratory Data", "Chemistry") == "Laboratory Data"

    def test_embedded_pairs_are_valid_only_as_pairs(self):
        names = taxonomy.load()
        assert names.is_valid_pair(PN, "Laboratory Data")
        assert names.is_valid_pair(PN, "Radiology Report")
        assert not names.is_valid_pair(PN, "Chemistry")

    def test_the_training_copies_match_the_pipeline_taxonomy(self):
        """The annotation tool and BERT training each keep their own copy, so
        they stand alone; all three must name the same classes."""
        pipeline = json.loads(
            (CORE / "stages/lib/keyword-canon/page_taxonomy.json").read_text(encoding="utf-8")
        )
        for folder in ("annotation-tool", "bert-training"):
            copy = REPO / "training" / folder / "taxonomy.json"
            assert json.loads(copy.read_text(encoding="utf-8")) == pipeline, folder


# --- keyword model ----------------------------------------------------------------


class TestKeywords:
    def test_a_title_in_the_title_zone_is_a_title_hit(self):
        hit = keywords.classify("DISCHARGE SUMMARY\nHospital course was uneventful.")
        assert hit.page_type == "Discharge Summary"
        assert hit.title_hit is True

    def test_the_same_title_far_below_the_title_zone_is_not_a_title_hit(self):
        body = "\n".join(f"line {i} of filler text" for i in range(30))
        hit = keywords.classify(body + "\nDischarge Summary\n")
        assert hit is not None and hit.page_type == "Discharge Summary"
        assert hit.title_hit is False

    def test_whole_words_only(self):
        assert keywords.classify("xdischarge summaryx") is None

    def test_ambiguous_terms_never_decide_alone(self):
        canon = json.loads((CORE / "stages/lib/keyword-canon/page_keyword_canon.json").read_text(encoding="utf-8"))
        ambiguous = next(t for s in canon["sub_types"] for t in s.get("ambiguous_terms") or [])
        hit = keywords.classify(ambiguous)
        assert hit is None or hit.score > canon["weights"]["ambiguous_term"]

    def test_a_title_inside_a_sentence_is_not_a_title(self):
        """"informed consent was obtained" is a sentence, not a Consent Form title."""
        hit = keywords.classify("OFFICE NOTE\nThe risks were explained and informed consent was obtained.")
        assert not (hit and hit.page_subtype == "Consent Form" and hit.title_hit)

    def test_a_title_used_as_a_heading_counts(self):
        assert keywords.classify("INFORMED CONSENT\nI agree to the procedure.").title_hit is True
        assert keywords.classify("OFFICE VISIT REPORT 10/03/2024\nmade up body").title_hit is True

    def test_a_layout_detected_heading_counts(self):
        text = "Patient Information Sheet Updated For The Year\nmade up body text"
        assert keywords.classify(text).title_hit is False
        assert keywords.classify(text, headings=["Patient Information Sheet Updated For The Year"]).title_hit is True

    def test_nothing_matched_is_none(self):
        assert keywords.classify("zzz qqq") is None
        assert keywords.classify("") is None


# --- level 1 and 2: the ladder -----------------------------------------------------


class TestLadder:
    def test_1_agreement(self):
        r = classify_page(bert("Discharge Summary", 0.6), kw("Discharge Summary"), ALL)
        assert (r.page_type, r.decided_by, r.needs_review) == ("Discharge Summary", "agreement", False)

    def test_2_bert_high(self):
        r = classify_page(bert("Discharge Summary", 0.95), kw("Chemistry"), ALL)
        assert (r.page_type, r.decided_by, r.needs_review) == ("Discharge Summary", "bert_high", False)

    def test_3_keyword_only_class(self):
        trained = ALL - {"Sleep Study"}
        r = classify_page(bert(PN, 0.35, lead=0.01), kw("Polysomnography", margin=0.5), trained)
        assert (r.page_type, r.decided_by) == ("Sleep Study", "keyword_only_class")

    def test_4_keyword_title(self):
        r = classify_page(bert(PN, 0.35, lead=0.01), kw("Chemistry", margin=3.0), ALL)
        assert (r.page_type, r.decided_by, r.needs_review) == ("Laboratory Data", "keyword_title", False)

    def test_5_bert_medium(self):
        r = classify_page(bert(PN, 0.35, lead=0.01), kw("Chemistry", margin=0.5, title_hit=False), ALL)
        assert (r.page_type, r.decided_by, r.needs_review) == (PN, "bert_medium", True)

    def test_6_keyword_body(self):
        r = classify_page(bert(PN, 0.15), kw("Chemistry", score=4, margin=0.5, title_hit=False), ALL)
        assert (r.page_type, r.decided_by, r.needs_review) == ("Laboratory Data", "keyword_body", True)

    def test_7_bert_low(self):
        r = classify_page(bert(PN, 0.15), kw("Chemistry", score=1, margin=0.5, title_hit=False), ALL)
        assert (r.page_type, r.decided_by, r.needs_review) == (PN, "bert_low", True)

    def test_no_model_and_no_keyword_is_unknown(self):
        r = classify_page(None, None, frozenset())
        assert (r.page_type, r.decided_by, r.needs_review) == ("Unknown", "no_prediction", True)

    def test_bert_below_10_percent_is_unknown(self):
        r = classify_page(bert(PN, 0.08), kw("Chemistry", score=4, margin=0.5, title_hit=False), ALL)
        assert (r.page_type, r.page_subtype, r.decided_by, r.needs_review) == ("Unknown", "Unknown", "unknown", True)
        assert r.codability is None and r.model_type is None

    def test_a_clear_keyword_still_wins_below_10_percent(self):
        r = classify_page(bert(PN, 0.08), kw("Chemistry", margin=3.0), ALL)
        assert (r.page_type, r.decided_by) == ("Laboratory Data", "keyword_title")

    def test_without_a_bert_model_unknown_does_not_apply(self):
        r = classify_page(None, kw("Chemistry", score=4, margin=0.5, title_hit=False), frozenset())
        assert r.page_type == "Laboratory Data"

    def test_between_10_and_25_percent_the_top_3_are_compared(self):
        b = bert(PN, 0.18, top=[PN, "Radiology Report", "Laboratory Data"])
        k = kw("Chemistry", score=2, margin=0.5, title_hit=False,
               top=["Laboratory Data", "Radiology Report", "Forms"])
        r = classify_page(b, k, ALL)
        # Radiology: ranks 1 + 1 = 2; Laboratory: 2 + 0 = 2 -> tie, BERT's order wins.
        assert (r.page_type, r.decided_by, r.needs_review) == ("Radiology Report", "top3_agreement", True)

    def test_no_shared_page_type_in_the_top_3_falls_through(self):
        b = bert(PN, 0.18, top=[PN, "Forms", "Orders"])
        k = kw("Chemistry", score=4, margin=0.5, title_hit=False, top=["Laboratory Data", "Radiology Report"])
        assert classify_page(b, k, ALL).decided_by == "keyword_body"

    def test_without_bert_the_keyword_model_still_decides(self):
        r = classify_page(None, kw("Discharge Summary"), frozenset())
        assert (r.page_type, r.decided_by) == ("Discharge Summary", "keyword_only_class")

    def test_level_2_progress_note_sub_type_comes_from_bert(self):
        r = classify_page(bert("Office Visit", 0.95), kw("Summary"), ALL)
        assert (r.page_type, r.page_subtype, r.model_type) == (PN, "Office Visit", "Office Visit")

    def test_level_2_other_sub_type_comes_from_keywords_else_generic(self):
        r = classify_page(bert("Laboratory Data", 0.95), kw("Chemistry"), ALL)
        assert r.page_subtype == "Chemistry"
        weak = classify_page(bert("Laboratory Data", 0.95), kw("Chemistry", score=1), ALL)
        assert weak.page_subtype == "Laboratory Data"

    def test_level_4_codability_from_the_page_type(self):
        assert classify_page(bert("Laboratory Data", 0.95), None, ALL).codability == "Non-Codable"
        assert classify_page(bert("Discharge Summary", 0.95), None, ALL).codability == "Discharge"

    def test_thresholds_come_from_the_file(self):
        assert thresholds() == {"bert_high": 0.50, "bert_low": 0.25, "bert_lead": 0.10,
                                "bert_unknown": 0.10, "keyword_min_score": 3.0,
                                "keyword_min_margin": 2.0}

    def test_2_bert_wins_at_50_percent(self):
        r = classify_page(bert("Discharge Summary", 0.55), kw("Chemistry"), ALL)
        assert (r.page_type, r.decided_by, r.needs_review) == ("Discharge Summary", "bert_high", False)

    def test_2_bert_wins_above_25_percent_with_a_clear_lead(self):
        r = classify_page(bert("Discharge Summary", 0.30, lead=0.12), kw("Chemistry"), ALL)
        assert (r.page_type, r.decided_by) == ("Discharge Summary", "bert_high")

    def test_without_a_clear_lead_25_percent_is_not_enough_against_a_keyword_title(self):
        r = classify_page(bert("Discharge Summary", 0.30, lead=0.06), kw("Chemistry", margin=3.0), ALL)
        assert (r.page_type, r.decided_by) == ("Laboratory Data", "keyword_title")


# --- level 3: the Final answer -------------------------------------------------------


def fp(pid, page_type, page_subtype=None, *, seq=None, link=None, confirmed=False,
       title=False, kw_sub=None, kw_score=None, conf=None, note=False):
    return FinalPage(pid, page_type, page_subtype or page_type, kw_sub, kw_score, title,
                     1 if seq else None, seq, link, confirmed, note, conf)


class TestAgreedPageType:
    """The page type is agreed across a document; the sub-type stays the page's."""

    def test_confirmed_first_page_sets_the_type_and_pages_keep_their_own_as_sub_type(self):
        doc = [fp(1, "Discharge Summary", seq=1, confirmed=True, title=True),
               fp(2, PN, "Office Visit", seq=2, link="strong", confirmed=True),
               fp(3, "Laboratory Data", "Chemistry", seq=3, link="strong", confirmed=True)]
        finals = [decide_final(p, doc) for p in doc]
        assert [f.page_type for f in finals] == ["Discharge Summary"] * 3
        assert [f.page_subtype for f in finals] == ["Discharge Summary", PN, "Laboratory Data"]
        assert [f.continuation_rule for f in finals] == ["agreed", "agreed_first_page", "agreed_first_page"]
        assert {f.codability for f in finals} == {taxonomy.load().codability("Discharge Summary")}
        assert not any(f.needs_review for f in finals)

    def test_lab_inside_a_progress_note_stays_non_codable(self):
        doc = [fp(1, PN, "Office Visit", seq=1, confirmed=True, title=True),
               fp(2, "Laboratory Data", "Chemistry", seq=2, link="strong", confirmed=True),
               fp(3, "Radiology Report", seq=3, link="strong", confirmed=True)]
        lab, rad = decide_final(doc[1], doc), decide_final(doc[2], doc)
        assert (lab.page_type, lab.page_subtype, lab.codability) == (PN, "Laboratory Data", "Non-Codable")
        assert (rad.page_type, rad.page_subtype, rad.codability) == (PN, "Radiology Report", "Codable")

    def test_without_a_confirmed_start_the_weighted_vote_decides_and_flags(self):
        doc = [fp(1, "Clinical Summary", seq=1, conf=0.40),
               fp(2, "Laboratory Data", seq=2, link="strong", conf=0.90),
               fp(3, "Laboratory Data", seq=3, link="strong", conf=0.80)]
        first = decide_final(doc[0], doc)
        assert (first.page_type, first.page_subtype) == ("Laboratory Data", "Clinical Summary")
        assert (first.continuation_rule, first.needs_review) == ("agreed_vote", True)
        assert decide_final(doc[1], doc).continuation_rule == "agreed"

    def test_no_clear_majority_keeps_each_page_and_flags_it(self):
        doc = [fp(1, "Clinical Summary", seq=1, conf=0.6), fp(2, PN, seq=2, link="strong", conf=0.7),
               fp(3, "Orders", seq=3, link="strong", conf=0.8)]
        finals = [decide_final(p, doc) for p in doc]
        assert [f.page_type for f in finals] == ["Clinical Summary", PN, "Orders"]
        assert all(f.continuation_rule == "no_agreement" and f.needs_review for f in finals)

    def test_the_keyword_sub_type_is_used_when_it_fits_the_agreed_type(self):
        doc = [fp(1, "Discharge Summary", seq=1, confirmed=True, title=True),
               fp(2, PN, seq=2, link="strong", confirmed=True, kw_sub="Depart Note", kw_score=3)]
        assert decide_final(doc[1], doc).page_subtype == "Depart Note"

    def test_an_unknown_page_takes_the_agreed_generic_sub_type(self):
        doc = [fp(1, "Discharge Summary", seq=1, confirmed=True, title=True), fp(2, "Unknown", seq=2, link="strong")]
        final = decide_final(doc[1], doc)
        assert (final.page_type, final.page_subtype) == ("Discharge Summary", "Discharge Summary")

    def test_a_weak_link_still_agrees_but_flags(self):
        doc = [fp(1, "Discharge Summary", seq=1, confirmed=True, title=True), fp(2, PN, seq=2, link="weak", confirmed=True)]
        final = decide_final(doc[1], doc)
        assert (final.page_type, final.needs_review) == ("Discharge Summary", True)

    def test_a_signed_progress_note_document_is_progress_note(self):
        doc = [fp(1, "Clinical Summary", seq=1, note=True, confirmed=True, title=True),
               fp(2, "Medication List", seq=2, link="strong", note=True)]
        finals = [decide_final(p, doc) for p in doc]
        assert [(f.page_type, f.page_subtype, f.codability) for f in finals] == [
            (PN, "Clinical Summary", "Codable"), (PN, "Medication List", "Non-Codable")]

    def test_a_page_alone_keeps_its_type(self):
        assert decide_final(fp(1, "Laboratory Data", "Chemistry"), None).page_type == "Laboratory Data"
        single = [fp(1, "Laboratory Data", seq=1)]
        assert decide_final(single[0], single).continuation_rule == "no_change"

    def test_embedded_same_page_needs_no_document(self):
        lab = fp(5, "Laboratory Data", kw_sub="Progress Note", kw_score=4, title=True)
        final = decide_final(lab, None)
        assert (final.page_type, final.continuation_rule, final.codability) == (PN, "embedded_same_page", "Non-Codable")


class TestPageTypeConfidence:
    def test_agreement_combines_both_models(self):
        r = classify_page(bert("Discharge Summary", 0.80), kw("Discharge Summary", margin=4.0), frozenset())
        assert r.decided_by == "agreement"
        # k = 0.60 + 0.05*4 = 0.80 -> 1 - 0.2*0.2
        assert r.confidence == pytest.approx(0.96)

    def test_bert_alone_is_bert_probability(self):
        r = classify_page(bert("Discharge Summary", 0.70), kw("Operative Report", title_hit=False, margin=1.0), frozenset())
        assert (r.decided_by, r.confidence) == ("bert_high", 0.70)

    def test_keyword_title_uses_the_keyword_confidence(self):
        r = classify_page(bert("Discharge Summary", 0.15), kw("Operative Report", margin=10.0), frozenset({"Discharge Summary"}))
        assert r.decided_by in ("keyword_title", "keyword_only_class")
        assert r.confidence == 0.90  # capped at keyword_title_cap

    def test_review_steps_are_capped_below_half(self):
        r = classify_page(bert("Discharge Summary", 0.45), kw("Operative Report", title_hit=False, margin=0.0), frozenset())
        assert r.decided_by == "bert_medium" and r.needs_review
        assert r.confidence == pytest.approx(0.45)
        unknown = classify_page(bert("Discharge Summary", 0.05), None, frozenset({"Discharge Summary"}))
        assert unknown.page_type == "Unknown" and unknown.confidence == pytest.approx(0.05)

    def test_no_prediction_has_no_confidence(self):
        assert classify_page(None, None, frozenset()).confidence is None


class TestProgressNoteRule:
    def test_each_page_becomes_progress_note_with_its_own_type_as_sub_type(self):
        lab = decide_final(FinalPage(2, "Laboratory Data", "Chemistry", in_progress_note=True), None)
        assert (lab.page_type, lab.page_subtype, lab.model_type) == (PN, "Laboratory Data", "Laboratory Data")
        assert (lab.source, lab.continuation_rule, lab.needs_review) == ("progress_note", "progress_note", False)
        # Inside a Progress Note a lab page stays Non-Codable.
        assert lab.codability == "Non-Codable"

    def test_codability_is_progress_notes_except_lab_and_medication_list(self):
        def final(page_type):
            return decide_final(FinalPage(2, page_type, page_type, in_progress_note=True), None).codability
        assert final("Clinical Summary") == taxonomy.load().codability(PN) == "Codable"
        assert final("Medication List") == "Non-Codable"
        assert final("Laboratory Data") == "Non-Codable"
        assert final("Radiology Report") == "Codable"

    def test_a_progress_note_page_keeps_its_sub_type(self):
        note = decide_final(FinalPage(1, PN, "Office Visit", in_progress_note=True), None)
        assert (note.page_type, note.page_subtype, note.source) == (PN, "Office Visit", "page")

    def test_an_unknown_page_is_progress_note_for_review(self):
        unknown = decide_final(FinalPage(3, "Unknown", "Unknown", in_progress_note=True), None)
        assert (unknown.page_type, unknown.page_subtype, unknown.needs_review) == (PN, PN, True)

    def test_it_wins_over_the_continuation_rules(self):
        start = FinalPage(1, "Discharge Summary", "Discharge Summary", keyword_title_hit=True, seq=1,
                          in_progress_note=True)
        page2 = FinalPage(2, PN, PN, seq=2, link_strength="strong", start_confirmed=True,
                          in_progress_note=True)
        assert decide_final(page2, [start, page2]).page_type == PN
        assert decide_final(start, [start, page2]).page_subtype == "Discharge Summary"


class TestModelFolder:
    def test_labels_that_are_not_model_types_fail_with_their_names(self, tmp_path):
        from stages.lib.page_classify.bert import ModelLabelsError, check_labels

        (tmp_path / "config.json").write_text(json.dumps({"id2label": {"0": "Progress Note", "1": "Specialty Note"}}), encoding="utf-8")
        with pytest.raises(ModelLabelsError, match="Specialty Note"):
            check_labels(tmp_path)

    def test_taxonomy_labels_pass(self, tmp_path):
        from stages.lib.page_classify.bert import check_labels

        (tmp_path / "config.json").write_text(json.dumps({"id2label": {"0": "Progress Note", "1": "Office Visit"}}), encoding="utf-8")
        assert check_labels(tmp_path) == {"Progress Note", "Office Visit"}

    def test_no_model_folder_runs_on_keywords_and_says_so(self, tmp_path, monkeypatch):
        import config
        from stages.lib.page_classify import bert as bert_module

        monkeypatch.setattr(config, "PAGE_FAMILY_MODEL_DIR", str(tmp_path / "missing"))
        status = bert_module.describe()
        assert status["ready"] is False and "keywords only" in status["reason"]
