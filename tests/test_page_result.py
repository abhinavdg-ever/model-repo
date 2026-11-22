"""The page classifier's answer (``result.PageResult``) and how later stages read it back; the
classifier's specialty, title and under-L1 outputs (2026-11-22: the page-axes cascade was
removed, ``leaf.predict`` is the one classifier). Made-up text only; no model (the keywords
decide alone), so no torch.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CORE = REPO / "core-pipeline"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from modules.page_classify import leaf  # noqa: E402
from modules.page_classify import result as page_result  # noqa: E402

LAB = "Lab Report"
RAD = "Radiology Report"
PN = "Progress Note"
DS_PAGE = """DISCHARGE SUMMARY
Hospital Course: uneventful stay, made up text.
Discharge Diagnosis: made up
Discharge Medications: made up list
"""
NO_TERMS = "zzz qqq\nnothing a canon term would match here\n"


@pytest.fixture(autouse=True)
def no_model(monkeypatch):
    """The keywords decide alone: no model folder is read."""
    monkeypatch.setattr(leaf, "model_side", lambda text, k, per_l1: leaf.ModelSide())


class TestPageNames:
    """page_type / page_subtype from the decided levels."""

    @pytest.mark.parametrize("levels, names", [
        ((LAB, RAD, "CT Scan Report"), ("Lab Report (Radiology Report)", "CT Scan Report")),
        ((LAB, RAD, None), (LAB, RAD)),
        ((PN, None, None), (PN, "Others")),
        ((None, None, None), (None, None)),
    ])
    def test_one_two_and_three_levels(self, levels, names):
        l1, l2, l3 = levels
        assert page_result.page_names(page_result.PageResult(l1=l1, l2=l2, l3=l3)) == names

    def test_no_answer_means_no_names(self):
        assert page_result.page_names(None) == (None, None)


class TestClassifier:
    def test_a_titled_page_is_its_family_with_its_title(self):
        result = leaf.predict(DS_PAGE)
        assert result.l1 == "Discharge Summary" and result.title_l1 == "Discharge Summary"
        assert result.canon_page_type is None  # retired with the page type canon
        assert result.leaf["model_source"] == leaf.NO_MODEL

    def test_under_every_other_l1_the_page_has_a_place_it_scored(self):
        text = DS_PAGE + "Chief Complaint: made up\nAssessment and Plan: made up\n"
        result = leaf.predict(text)
        assert result.l1 == "Discharge Summary" and "Discharge Summary" not in result.under_l1
        for l1, there in result.under_l1.items():
            assert there["l2"] in page_result.l2_names(l1)

    def test_nothing_on_the_page_is_others(self):
        result = leaf.predict(NO_TERMS)
        assert result.l1 == "Others" and result.decided_by == page_result.BY_OTHERS
        assert result.specialty == "General"

    def test_no_text_is_no_answer(self):
        assert leaf.predict("  ") is None


class TestSpecialty:
    """The model's specialty head, else a specialty title, else the specialty keywords, else General."""

    def test_a_sure_model_specialty_is_asserted(self):
        model = leaf.ModelSide(specialty=("Cardiology", 0.8))
        assert leaf.page_specialty(model, NO_TERMS, ()) == ("Cardiology", 0.8, page_result.MODEL)

    @pytest.mark.parametrize("spec", [("Cardiology", 0.3), ("Unknown", 0.99), ("Others", 0.9)])
    def test_an_unsure_or_unknown_model_specialty_is_not(self, spec):
        found, _conf, who = leaf.page_specialty(leaf.ModelSide(specialty=spec), NO_TERMS, ())
        assert (found, who) == ("General", page_result.BY_OTHERS)

    def test_a_specialty_title_in_the_title_zone(self):
        found, _conf, who = leaf.page_specialty(leaf.ModelSide(), "Audiology Report\nmade up body", ())
        assert (found, who) == ("Audiology", page_result.CANON)

    def test_unknown_is_never_asserted_it_is_general(self):
        assert leaf.page_specialty(leaf.ModelSide(), NO_TERMS, ())[0] == "General"


class TestStageRow:
    def test_the_answer_becomes_the_row(self):
        from modules.page_classify.stage import _page_row

        row = _page_row(leaf.predict(DS_PAGE))
        assert row["page_type"].startswith("Discharge Summary")
        assert row["axes"]["l1"] == "Discharge Summary"
        assert not any(key.startswith(("bert_", "keyword_")) for key in row)  # those columns stay empty

    def test_nothing_decided_is_others_for_review(self):
        from modules.page_classify.stage import _page_row

        row = _page_row(leaf.predict(NO_TERMS))
        assert (row["page_type"], row["page_subtype"]) == ("Others", "Others")
        assert row["needs_review"] is True and row["decided_by"] == "others"

    def test_discharge_codeability_is_the_discharge_category(self):
        from modules.page_classify.stage import _page_row

        row = _page_row(page_result.PageResult(l1="Discharge Summary", l1_confidence=0.9, codeability="discharge"))
        assert (row["page_type"], row["page_subtype"]) == ("Discharge Summary", "Others")
        assert row["classification_category"] == "discharge_summary"

    def test_csv_columns_are_unchanged(self):
        """The codeable CSV keeps its axes_* columns (a frozen contract), canon_page_type included."""
        from modules.page_classify.stage import CODEABLE_COLS

        cols = set(page_result.PageResult().csv_columns())
        assert cols <= set(CODEABLE_COLS) and "axes_canon_page_type" in cols


class TestStorage:
    def test_every_written_column_is_in_the_schema(self):
        from common.db import PAGE_CLASSIFICATION_FIELDS

        sql = (REPO / "schema" / "schema.sql").read_text(encoding="utf-8")
        table = re.search(r"CREATE TABLE page_classification \((.*?)\n\);", sql, re.S).group(1)
        columns = set(re.findall(r"^\s+([a-z_0-9]+)\s+[A-Z]", table, re.M))
        assert set(PAGE_CLASSIFICATION_FIELDS) <= columns
        assert "axes" in PAGE_CLASSIFICATION_FIELDS


class TestDownstreamNames:
    """Stages after classification read the stored row's names."""

    ROW = {"page_type": "Patient Communication", "page_subtype": "Telephone Encounter",
           "axes": {"l1": "Patient Communication", "l2": "Telephone Encounter", "title_l1": "Patient Communication"}}

    def test_names_are_most_specific_first(self):
        assert page_result.names_of(self.ROW) == ("Telephone Encounter", "Patient Communication")
        assert page_result.family_of(self.ROW) == "Patient Communication"
        assert page_result.title_of(self.ROW) == "Patient Communication"

    def test_rows_written_before_the_retirement_still_read(self):
        old = {"axes": {"l1": PN, "canon_page_type": "Telephone Encounter"}}
        assert page_result.names_of(old)[0] == "Telephone Encounter"

    def test_json_text_from_the_driver_reads_the_same(self):
        assert page_result.names_of({**self.ROW, "axes": json.dumps(self.ROW["axes"])})[0] == "Telephone Encounter"

    def test_a_row_without_axes_falls_back_to_its_columns(self):
        assert page_result.names_of({"page_type": "A", "page_subtype": "B"}) == ("B", "A")

    def test_encounter_tier_one_finds_the_telephone_encounter(self):
        from modules.encounter.encounter_classify import load_canon

        tier1 = load_canon().tier1
        assert next(n for n in page_result.names_of(self.ROW) if n in tier1) == "Telephone Encounter"
        assert tier1["Telephone Encounter"] == "outpatient_tele"

    def test_every_encounter_tier_one_name_is_a_taxonomy_name(self):
        from modules.encounter.encounter_classify import load_canon

        assert set(load_canon().tier1) <= page_result.names()

    def test_default_dos_types_match_page_names(self):
        from modules.continuity.engine import uses_default_final_dos

        demographics = {"axes": {"l1": "Demographics", "l2": "Patient Information"}}
        assert uses_default_final_dos(*page_result.names_of(demographics))
        assert not uses_default_final_dos(*page_result.names_of({"axes": {"l1": PN}}))


class TestStaging:
    def test_written_atomically_and_read_back_by_page_name(self, tmp_path, monkeypatch):
        from modules.page_classify import stage

        monkeypatch.setattr(stage, "staging_dir", lambda chart: tmp_path / chart / "staging")
        result = leaf.predict(DS_PAGE)
        entry = stage._staged_entry(stage._page_row(result), result, "final1")
        path = stage.write_staged("chart1", {"chart1_Pg1": entry})
        assert path == tmp_path / "chart1" / "staging" / "page_classification.json"
        assert not path.with_suffix(".tmp").exists()
        pages = stage.read_staged("chart1")
        assert pages["chart1_Pg1"]["axes"]["l1"] == "Discharge Summary"

    def test_a_missing_or_foreign_file_reads_empty(self, tmp_path, monkeypatch):
        from modules.page_classify import stage

        monkeypatch.setattr(stage, "staging_dir", lambda chart: tmp_path / chart / "staging")
        assert stage.read_staged("nothing") == {}
        (tmp_path / "old" / "staging").mkdir(parents=True)
        (tmp_path / "old" / "staging" / "page_classification.json").write_text('{"version": 0}', encoding="utf-8")
        assert stage.read_staged("old") == {}


class TestSpecialtyLexicon:
    def test_the_pipeline_copy_is_the_training_lexicon(self):
        pipeline = CORE / "resources" / "canon" / "specialty_canon.json"
        training = REPO / "training" / "taxonomy" / "rules" / "specialty.json"
        if not training.is_file():
            pytest.skip("training/ is kept on the dev machine only")
        assert json.loads(pipeline.read_text(encoding="utf-8")) == json.loads(training.read_text(encoding="utf-8"))

    def test_every_lexicon_specialty_is_in_the_label_space(self):
        from modules.page_classify import taxonomy

        lexicon = json.loads((CORE / "resources" / "canon" / "specialty_canon.json").read_text(encoding="utf-8"))
        assert set(lexicon["specialties"]) <= set(taxonomy.label_space()["specialty"])


class TestEncounterSpecialty:
    """imaging_final: only the first page of the encounter decides its specialty."""

    @staticmethod
    def _rows(*specialties):
        return {i: {"axes": {"specialty": sp}} for i, sp in enumerate(specialties, start=1)}

    def test_the_first_page_decides_for_every_page(self):
        from modules.imaging_final.stage import encounter_specialties

        out = encounter_specialties({1: [1, 2, 3]}, self._rows("Cardiology", "Nephrology", "Nephrology"))
        assert out == {1: "Cardiology", 2: "Cardiology", 3: "Cardiology"}

    def test_a_first_page_with_none_gives_general_even_when_later_pages_have_one(self):
        from modules.imaging_final.stage import encounter_specialties

        out = encounter_specialties({1: [1, 2], 2: [3]}, self._rows(None, "Cardiology", "Unknown"))
        assert out == {1: "General", 2: "General", 3: "General"}


class TestOneCanon:
    """2026-11-22 (user): one keyword canon for the whole of page classification."""

    def test_the_retired_files_are_gone(self):
        canon = CORE / "resources" / "canon"
        for name in ("page_canon.json", "leaf_keywords.json", "page_keyword_weights.json", "class_keywords.json"):
            assert not (canon / name).exists(), name
        for name in ("axes.py", "axes_model.py", "keywords.py", "keyword_vote.py", "page_rules.py"):
            assert not (CORE / "modules" / "page_classify" / name).exists(), name

    def test_every_taxonomy_node_has_keyword_lists(self):
        from modules.page_classify import leaf_canon

        assert set(leaf_canon.canon().nodes) == set(leaf_canon.taxonomy_nodes())
