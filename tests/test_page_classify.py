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


def bert(model_type, confidence, lead=0.0):
    return BertResult(model_type, taxonomy.load().page_type_of_model(model_type), confidence, lead)


def kw(page_subtype, score=5.0, margin=4.0, title_hit=True):
    names = taxonomy.load()
    page_type = names.page_type_of_subtype(page_subtype)
    return KeywordResult(page_subtype, page_type, names.model_type(page_type, page_subtype),
                         score, margin, title_hit)


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

    def test_no_model_and_no_keyword_is_no_prediction(self):
        r = classify_page(None, None, frozenset())
        assert (r.page_type, r.decided_by, r.needs_review) == (None, "no_prediction", True)

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
        assert thresholds() == {"bert_high": 0.50, "bert_low": 0.25, "bert_lead": 0.05,
                                "keyword_min_score": 3.0, "keyword_min_margin": 2.0}

    def test_2_bert_wins_at_50_percent(self):
        r = classify_page(bert("Discharge Summary", 0.55), kw("Chemistry"), ALL)
        assert (r.page_type, r.decided_by, r.needs_review) == ("Discharge Summary", "bert_high", False)

    def test_2_bert_wins_above_25_percent_with_a_clear_lead(self):
        r = classify_page(bert("Discharge Summary", 0.30, lead=0.06), kw("Chemistry"), ALL)
        assert (r.page_type, r.decided_by) == ("Discharge Summary", "bert_high")

    def test_without_a_clear_lead_25_percent_is_not_enough_against_a_keyword_title(self):
        r = classify_page(bert("Discharge Summary", 0.30, lead=0.02), kw("Chemistry", margin=3.0), ALL)
        assert (r.page_type, r.decided_by) == ("Laboratory Data", "keyword_title")


# --- level 3: the Final answer -------------------------------------------------------


def fp(pid, page_type, page_subtype=None, *, seq=None, link=None, confirmed=False,
       title=False, kw_sub=None, kw_score=None):
    return FinalPage(pid, page_type, page_subtype or page_type, kw_sub, kw_score, title,
                     1 if seq else None, seq, link, confirmed)


class TestContinuationRules:
    def test_acceptance_discharge_summary_chart(self):
        """Discharge Summary title page, untitled page 2 of 3, lab page →
        Discharge Summary, Discharge Summary, Laboratory Data."""
        start = fp(1, "Discharge Summary", seq=1, confirmed=True, title=True)
        page2 = fp(2, PN, seq=2, link="strong", confirmed=True)
        lab = fp(3, "Laboratory Data", seq=3, link="strong", confirmed=True, title=True)
        finals = [decide_final(start, None), decide_final(page2, start), decide_final(lab, start)]
        assert [f.page_type for f in finals] == ["Discharge Summary", "Discharge Summary", "Laboratory Data"]
        assert finals[1].continuation_rule == "continuation"
        assert finals[1].page_subtype == "Discharge Summary"

    def test_acceptance_lab_page_inside_a_progress_note(self):
        start = fp(1, PN, "Office Visit", seq=1, confirmed=True, title=True)
        lab = fp(2, "Laboratory Data", "Chemistry", seq=2, link="strong", confirmed=True)
        final = decide_final(lab, start)
        assert (final.page_type, final.page_subtype, final.model_type) == (PN, "Laboratory Data", "Laboratory Data")
        assert final.codability == "Codable"
        assert final.continuation_rule == "embedded_in_document"

    def test_embedded_same_page_needs_no_order(self):
        lab = fp(5, "Laboratory Data", kw_sub="Progress Note", kw_score=4, title=True)
        final = decide_final(lab, None)
        assert (final.page_type, final.continuation_rule) == (PN, "embedded_same_page")

    def test_weak_link_keeps_the_page_and_flags_it(self):
        start = fp(1, "Discharge Summary", seq=1, confirmed=True, title=True)
        page2 = fp(2, PN, seq=2, link="weak", confirmed=True)
        final = decide_final(page2, start)
        assert (final.page_type, final.continuation_rule, final.needs_review) == (PN, "possible", True)

    def test_unconfirmed_start_keeps_the_page_and_flags_it(self):
        start = fp(1, "Discharge Summary", seq=1, confirmed=False)
        page2 = fp(2, PN, seq=2, link="strong", confirmed=False)
        assert decide_final(page2, start).continuation_rule == "possible"

    def test_a_titled_page_is_never_moved(self):
        start = fp(1, "Discharge Summary", seq=1, confirmed=True, title=True)
        page2 = fp(2, PN, "Office Visit", seq=2, link="strong", confirmed=True, title=True)
        assert decide_final(page2, start).page_type == PN

    def test_a_progress_note_continuing_a_progress_note_stays(self):
        start = fp(1, PN, seq=1, confirmed=True, title=True)
        page2 = fp(2, PN, seq=2, link="strong", confirmed=True)
        assert decide_final(page2, start).continuation_rule == "no_change"

    def test_a_stand_alone_lab_page_keeps_its_type(self):
        assert decide_final(fp(1, "Laboratory Data", "Chemistry"), None).page_type == "Laboratory Data"

    def test_continuation_takes_the_keyword_sub_type_when_it_fits(self):
        start = fp(1, "Discharge Summary", seq=1, confirmed=True, title=True)
        page2 = fp(2, PN, seq=2, link="strong", confirmed=True, kw_sub="Depart Note", kw_score=3)
        assert decide_final(page2, start).page_subtype == "Depart Note"

    def test_every_returned_pair_is_a_taxonomy_or_embedded_pair(self):
        names = taxonomy.load()
        start_ds = fp(1, "Discharge Summary", seq=1, confirmed=True, title=True)
        start_pn = fp(1, PN, seq=1, confirmed=True, title=True)
        cases = [
            decide_final(fp(2, PN, seq=2, link="strong", confirmed=True), start_ds),
            decide_final(fp(2, "Radiology Report", seq=2, link="strong", confirmed=True), start_pn),
            decide_final(fp(2, "Laboratory Data", kw_sub="Progress Note", title=True), None),
        ]
        for final in cases:
            assert names.is_valid_pair(final.page_type, final.page_subtype), final


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
