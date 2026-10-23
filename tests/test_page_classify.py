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
        assert len(names.page_types) == 51
        assert len(names.subtype_page_type) == 191
        assert len(names.model_types) == 60
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
        """The annotation tool uses taxonomy v24, whose copy must equal the one
        build_taxonomy.py writes; the page-axes trainer uses taxonomy v29, whose
        copy must equal the one build_taxonomy_v29.py writes. (The single-head
        trainer and its taxonomy.json copy were removed on 2026-10-23.)"""
        v24 = [REPO / "training" / folder / "taxonomy_v24.json" for folder in ("taxonomy", "annotation-tool")]
        texts = {path.read_text(encoding="utf-8") for path in v24}
        assert len(texts) == 1, "run training/taxonomy/build_taxonomy.py"
        v29 = [REPO / "training" / folder / "taxonomy_v29.json" for folder in ("taxonomy", "bert-training")]
        assert len({path.read_text(encoding="utf-8") for path in v29}) == 1, "run training/taxonomy/build_taxonomy_v29.py"
        canon = [REPO / "training" / folder / "page_canon_v24.json" for folder in ("taxonomy", "annotation-tool")]
        assert len({path.read_text(encoding="utf-8") for path in canon}) == 1, "run training/taxonomy/build_canon.py"


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


# --- level 3: the Final answer (v24 names) -----------------------------------------

LAB = "Lab Report/Result"
DS = "Discharge Summary"


def fp(pid, page_type, page_subtype=None, *, family=None, names=(), category="codeable", rule="page_type_tag",
       title=None, seq=None, link=None, conf=None, l2=None):
    return FinalPage(pid, page_type, page_subtype or page_type, family=family, names=names, l2=l2, category=category,
                     codeability_rule=rule, title_l1=title, model_type=page_type,
                     document_seq=1 if seq else None, seq=seq, link_strength=link, confidence=conf)


class TestAgreedPageType:
    """Every page takes the first page's family (L1); its L2 is one the hierarchy places under it."""

    def test_the_first_page_sets_the_family_and_a_foreign_page_is_others(self):
        doc = [fp(1, DS, "Depart/Discharge Note", category="discharge_summary", seq=1),
               fp(2, PN, seq=2, link="strong"),
               fp(3, f"{LAB} (Laboratory)", "Chemistry Panel", family=LAB, category="non_codeable",
                  names=("Chemistry Panel", "Laboratory", LAB), l2="Laboratory", seq=3, link="strong")]
        finals = [decide_final(p, doc) for p in doc]
        assert [f.page_type for f in finals] == [DS] * 3
        # Neither a Progress Note nor a lab page has a place under Discharge Summary.
        assert [f.page_subtype for f in finals] == ["Depart/Discharge Note", "Others", "Others"]
        assert [f.continuation_rule for f in finals] == ["agreed", "agreed_first_page", "agreed_first_page"]
        assert {f.codability for f in finals} == {"Discharge"}
        assert not any(f.needs_review for f in finals)

    def test_a_page_of_the_same_family_keeps_its_own_names_and_codability(self):
        doc = [fp(1, f"{LAB} (Radiology)", "CT Scan Report", family=LAB, category="non_codeable", seq=1),
               fp(2, f"{LAB} (Laboratory)", "Chemistry Panel", family=LAB, category="non_codeable",
                  seq=2, link="strong")]
        second = decide_final(doc[1], doc)
        assert (second.page_type, second.page_subtype, second.continuation_rule) == (
            f"{LAB} (Laboratory)", "Chemistry Panel", "agreed")
        assert second.codability == "Non-Codable"

    def test_lab_inside_a_progress_note_stays_non_codable_radiology_does_not(self):
        doc = [fp(1, PN, seq=1),
               fp(2, f"{LAB} (Laboratory)", "Chemistry Panel", family=LAB, category="non_codeable",
                  names=("Chemistry Panel", "Laboratory", LAB), l2="Laboratory", seq=2, link="strong"),
               fp(3, f"{LAB} (Radiology)", "CT Scan Report", family=LAB, category="non_codeable",
                  names=("CT Scan Report", "Radiology", LAB), l2="Radiology", seq=3, link="strong")]
        lab, rad = decide_final(doc[1], doc), decide_final(doc[2], doc)
        assert (lab.page_type, lab.page_subtype, lab.codability) == (PN, "Laboratory", "Non-Codable")
        assert (rad.page_type, rad.page_subtype, rad.codability) == (PN, "Radiology", "Codable")

    def test_a_medication_list_inside_a_progress_note_is_progress_note_medication_list(self):
        doc = [fp(1, PN, seq=1),
               fp(2, "Medication Record", "Medication List", family="Medication Record",
                  names=("Medication List", "Medication Record"), l2="Medication List", seq=2, link="strong"),
               fp(3, "Administrative", "Billing Statement", family="Administrative",
                  names=("Billing Statement", "Administrative"), l2="Billing Statement", seq=3, link="strong")]
        med, bill = decide_final(doc[1], doc), decide_final(doc[2], doc)
        assert (med.page_type, med.page_subtype, med.codability) == (PN, "Medication List", "Non-Codable")
        assert (bill.page_type, bill.page_subtype) == (PN, "Others")

    def test_a_page_whose_own_l2_belongs_under_the_document_keeps_it(self):
        doc = [fp(1, "Medication Record", "Medication List", family="Medication Record", seq=1),
               fp(2, PN, "Medication Reconciliation", family=PN,
                  names=("Medication Reconciliation", PN), seq=2, link="strong")]
        final = decide_final(doc[1], doc)
        assert (final.page_type, final.page_subtype) == ("Medication Record", "Medication Reconciliation")

    def test_a_context_page_takes_its_documents_codability(self):
        doc = [fp(1, "Problem List", seq=1, category="codeable"),
               fp(2, "Problem List", "Clinical Summary", category="non_codeable", rule="context",
                  seq=2, link="strong")]
        assert decide_final(doc[1], doc).codability == "Codable"
        alone = fp(5, "Problem List", "Clinical Summary", category="non_codeable", rule="context")
        assert decide_final(alone, None).codability == "Non-Codable"

    def test_the_first_pages_family_wins_even_outvoted(self):
        doc = [fp(1, "Problem List", seq=1, conf=0.40),
               fp(2, LAB, family=LAB, seq=2, link="strong", conf=0.90),
               fp(3, LAB, family=LAB, seq=3, link="strong", conf=0.80)]
        finals = [decide_final(p, doc) for p in doc]
        assert [f.page_type for f in finals] == ["Problem List"] * 3
        assert finals[1].continuation_rule == "agreed_first_page"

    def test_an_unknown_first_page_falls_back_to_the_weighted_vote(self):
        doc = [fp(1, "Unknown", seq=1, conf=0.40),
               fp(2, f"{LAB} (Laboratory)", "Chemistry Panel", family=LAB, seq=2, link="strong", conf=0.90),
               fp(3, LAB, "Radiology", family=LAB, seq=3, link="strong", conf=0.80)]
        first = decide_final(doc[0], doc)
        assert (first.page_type, first.page_subtype, first.continuation_rule, first.needs_review) == (
            LAB, "Others", "agreed_vote", True)

    def test_no_clear_majority_keeps_each_page_and_flags_it(self):
        doc = [fp(1, "Unknown", seq=1, conf=0.6), fp(2, PN, seq=2, link="strong", conf=0.7),
               fp(3, "Administrative", seq=3, link="strong", conf=0.8), fp(4, "Problem List", seq=4, conf=0.75)]
        finals = [decide_final(p, doc) for p in doc]
        assert [f.page_type for f in finals] == ["Unknown", PN, "Administrative", "Problem List"]
        assert all(f.continuation_rule == "no_agreement" and f.needs_review for f in finals)

    def test_a_weak_link_still_agrees_but_flags(self):
        doc = [fp(1, DS, category="discharge_summary", seq=1), fp(2, PN, seq=2, link="weak")]
        final = decide_final(doc[1], doc)
        assert (final.page_type, final.needs_review) == (DS, True)

    def test_a_page_alone_keeps_its_type(self):
        page = fp(1, f"{LAB} (Radiology)", "CT Scan Report", family=LAB, category="non_codeable")
        final = decide_final(page, None)
        assert (final.page_type, final.codability, final.continuation_rule) == (
            f"{LAB} (Radiology)", "Non-Codable", "no_change")

    def test_embedded_same_page_needs_no_document(self):
        lab = fp(5, f"{LAB} (Laboratory)", "Chemistry Panel", family=LAB, category="non_codeable",
                 names=("Chemistry Panel", "Laboratory", LAB), l2="Laboratory", title=PN)
        final = decide_final(lab, None)
        assert (final.page_type, final.page_subtype, final.continuation_rule, final.codability) == (
            PN, "Laboratory", "embedded_same_page", "Non-Codable")
        rad = fp(6, f"{LAB} (Radiology)", "CT Scan Report", family=LAB, names=("Radiology", LAB), title=PN)
        assert decide_final(rad, None).codability == "Codable"


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


class TestFinalHierarchy:
    """When the document's L1 differs, L2 / L3 come from the page's own scores under that L1."""

    def test_the_page_takes_its_l2_and_l3_under_the_new_l1(self):
        doc = [fp(1, f"{LAB} (Radiology)", "CT Scan Report", family=LAB, seq=1),
               fp(2, PN, "Others", family=PN, seq=2, link="strong")]
        doc[1] = FinalPage(**{**doc[1].__dict__, "under_l1": {
            LAB: {"l2": "Laboratory", "l3": "Chemistry Report"}}})
        final = decide_final(doc[1], doc)
        assert (final.page_type, final.page_subtype) == (f"{LAB} (Laboratory)", "Chemistry Report")

    def test_nothing_under_the_new_l1_is_others(self):
        doc = [fp(1, "Medication Record", "Medication List", family="Medication Record", seq=1),
               fp(2, "Administrative", "Billing Statement", family="Administrative",
                  names=("Billing Statement", "Administrative"), seq=2, link="strong")]
        final = decide_final(doc[1], doc)
        assert (final.page_type, final.page_subtype) == ("Medication Record", "Others")

    def test_an_l1_is_never_its_own_sub_type(self):
        assert decide_final(fp(1, PN), None).page_subtype == "Others"
