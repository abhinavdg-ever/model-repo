"""Page axes: the inference copy of train_axes.py, and the gate that decides
each level by the model or the v24 keyword canon.

Made-up text only. No torch: the gate takes the model's raw answer as a dict.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CORE = REPO / "core-pipeline"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from stages.lib.page_classify import axes  # noqa: E402

TRAINER = REPO / "training" / "bert-training" / "train_axes.py"
COPY = CORE / "stages" / "lib" / "page_classify" / "axes_model.py"

LAB = "Lab Report/Result"
CT_PAGE = """CT ABDOMEN PELVIS WITH CONTRAST
COMPUTED TOMOGRAPHY
Technique: axial images were acquired. Hounsfield units were measured.
IMPRESSION: no acute findings
"""
NO_TERMS = "zzz qqq\nnothing a canon term would match here\n"

SPACE = {
    "other_L1": "OTHER",
    "train_pages": {"specialty": {"Cardiology": 40, "Audiology": 5, "Unknown": 600}},
}


def raw(l1=LAB, l1_p=0.9, second=0.05, l2="Radiology", l2_p=0.9, l3=None, l3_p=None,
        specialty_top=(("Unknown", 0.9), ("Cardiology", 0.05)), codeability="non_codeable", code_p=0.9):
    return {
        "L1": l1, "L1_confidence": l1_p, "L1_top": [(l1, l1_p), ("Progress Note", second)],
        "L2": l2, "L2_confidence": l2_p, "L3": l3, "L3_confidence": l3_p,
        "specialty": specialty_top[0][0] if specialty_top else None,
        "specialty_confidence": specialty_top[0][1] if specialty_top else None,
        "specialty_top": list(specialty_top),
        "codeability": codeability, "codeability_confidence": code_p,
        "path": l1,
    }


def gate(r, text=CT_PAGE, space=SPACE):
    return axes.gate(r, space, text)


class TestInferenceCopy:
    """axes_model.py is train_axes.py's inference code, copied unchanged."""

    @staticmethod
    def _definitions(path: Path) -> dict[str, str]:
        source = path.read_text(encoding="utf-8")
        out = {}
        for node in ast.parse(source).body:
            if isinstance(node, ast.FunctionDef):
                out[node.name] = ast.get_source_segment(source, node)
            elif isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
                out[node.targets[0].id] = ast.get_source_segment(source, node)
        return out

    def test_every_copied_definition_matches_the_trainer(self):
        trainer, copy = self._definitions(TRAINER), self._definitions(COPY)
        drifted = [name for name, body in copy.items() if trainer.get(name) != body]
        assert not drifted, f"copy these from train_axes.py into axes_model.py again: {drifted}"

    def test_the_copy_names_nothing_it_does_not_define(self):
        copy = self._definitions(COPY)
        used = set(re.findall(r"\b([A-Z_]{3,}|[a-z_]+)\(", COPY.read_text(encoding="utf-8")))
        trainer = self._definitions(TRAINER)
        missing = sorted(n for n in used if n in trainer and n not in copy)
        assert not missing, f"axes_model.py calls trainer functions it does not copy: {missing}"


class TestLevelOne:
    def test_model_decides_at_forty_percent_with_a_lead(self):
        result = gate(raw(l1_p=0.42, second=0.20))
        assert (result.route, result.l1, result.sources[0]) == ("model", LAB, "model")

    def test_below_forty_percent_the_canon_decides(self):
        result = gate(raw(l1="Administrative", l1_p=0.38, second=0.30))
        assert result.route == "canon"
        assert (result.l1, result.sources[0]) == (LAB, "canon")
        assert result.l1_confidence >= 0.5

    def test_no_lead_means_the_canon_decides(self):
        assert gate(raw(l1_p=0.45, second=0.40)).sources[0] == "canon"

    def test_folded_other_class_goes_to_the_canon(self):
        result = gate(raw(l1="OTHER", l1_p=0.95))
        assert (result.route, result.l1) == ("canon", LAB)

    def test_nothing_sure_leaves_it_empty(self):
        result = gate(raw(l1="Administrative", l1_p=0.2, second=0.1), text=NO_TERMS)
        assert result.route == "review"
        assert (result.l1, result.l2, result.l3) == (None, None, None)
        assert result.sources[:3] == ("-", "-", "-")


class TestLevelTwoAndThree:
    def test_model_l2_at_fifty_percent_and_l3_from_the_canon(self):
        result = gate(raw(l2_p=0.51, l3="MRI Report", l3_p=0.99))
        assert (result.l2, result.sources[1]) == ("Radiology", "model")
        assert (result.l3, result.sources[2]) == ("CT Scan Report", "canon")
        assert result.l3_confidence >= 0.5
        assert result.prediction["L3"] == "MRI Report"  # the model's L3 is kept, not used

    def test_low_model_l2_falls_back_to_the_canon(self):
        result = gate(raw(l2="Laboratory", l2_p=0.45))
        assert (result.l2, result.sources[1]) == ("Radiology", "canon")

    def test_l3_needs_the_canon_to_be_sure(self):
        result = gate(raw(l3="CT Scan Report", l3_p=0.99), text="RADIOLOGY\nresults below\n")
        assert result.l2 == "Radiology"
        assert (result.l3, result.sources[2]) == (None, "-")

    def test_section_l1_has_no_l2_or_l3(self):
        result = gate(raw(l1="Progress Note", l2=None))
        assert result.l1 == "Progress Note"
        assert (result.l2, result.l3) == (None, None)


class TestSpecialty:
    def test_unknown_is_never_asserted(self):
        assert gate(raw()).specialty is None

    def test_confident_well_trained_specialty_is_asserted(self):
        result = gate(raw(specialty_top=(("Cardiology", 0.85), ("Unknown", 0.10))))
        assert (result.specialty, result.specialty_confidence, result.sources[3]) == ("Cardiology", 0.85, "model")

    @pytest.mark.parametrize("top", [
        (("Cardiology", 0.65), ("Unknown", 0.05)),   # below 0.70
        (("Cardiology", 0.75), ("Nursing", 0.60)),   # lead below 0.20
        (("Audiology", 0.95), ("Unknown", 0.01)),    # 5 training pages, needs 10
    ])
    def test_unsure_or_undertrained_specialty_is_not_asserted(self, top):
        result = gate(raw(specialty_top=top))
        assert (result.specialty, result.specialty_confidence) == (None, None)


class TestCodeability:
    """Codable / Non-Codable / Discharge always comes from the canon."""

    def test_the_model_never_decides_it(self):
        result = gate(raw(codeability="codeable", code_p=0.99))
        assert (result.codeability, result.sources[4]) == ("non_codeable", "canon")
        assert result.prediction["codeability"] == "codeable"  # kept, not used

    def test_the_canon_page_type_behind_the_path_gives_the_tag(self):
        result = gate(raw(l2_p=0.41))
        assert result.canon_page_type == "CT Scan Report"
        assert (result.codeability, result.codeability_rule) == ("non_codeable", "page_type_tag")

    def test_a_context_rule_waits_for_the_document(self):
        result = axes.gate(None, {}, "CLINICAL SUMMARY\nproblems and medications reviewed\n")
        assert result.codeability_rule == "context"
        assert (result.codeability, result.codeability_confidence) == ("needs_review", None)

    def test_nothing_decided_means_needs_review(self):
        result = gate(raw(l1="Administrative", l1_p=0.2, second=0.1), text=NO_TERMS)
        assert (result.codeability, result.codeability_confidence) == ("needs_review", None)


class TestWithoutModel:
    def test_canon_decides_every_level(self):
        result = axes.gate(None, {}, CT_PAGE)
        assert result.route is None
        assert (result.l1, result.l2, result.l3) == (LAB, "Radiology", "CT Scan Report")
        assert result.as_json()["sources"] == "canon/canon/canon/-/canon"
        assert result.as_json()["prediction"] is None

    def test_missing_folder_reports_canon_only(self, tmp_path, monkeypatch):
        import config

        monkeypatch.setattr(config, "PAGE_AXES_MODEL_DIR", str(tmp_path / "missing"))
        status = axes.describe()
        assert status["ready"] is False and "canon only" in status["reason"]

    def test_model_on_another_taxonomy_is_refused(self, tmp_path):
        with pytest.raises(axes.ModelTaxonomyError, match="v23"):
            axes.check_taxonomy({"taxonomy_version": "v23"}, tmp_path)


class TestPageNames:
    """page_type / page_subtype from the decided levels."""

    @pytest.mark.parametrize("levels, names", [
        ((LAB, "Radiology", "CT Scan Report"), ("Lab Report/Result (Radiology)", "CT Scan Report")),
        ((LAB, "Radiology", None), (LAB, "Radiology")),
        (("Progress Note", None, None), ("Progress Note", "Others")),
        ((None, None, None), (None, None)),
    ])
    def test_one_two_and_three_levels(self, levels, names):
        l1, l2, l3 = levels
        assert axes.page_names(axes.AxesResult(l1=l1, l2=l2, l3=l3)) == names

    def test_under_every_other_l1_the_page_has_a_place(self):
        result = axes.gate(None, {}, CT_PAGE)
        assert result.l1 == LAB and LAB not in result.under_l1
        assert "Progress Note" not in result.under_l1  # its L2 is the content section
        for l1, there in result.under_l1.items():
            assert there["l2"] in axes.l2_names(l1)

    def test_no_axes_means_no_names(self):
        assert axes.page_names(None) == (None, None)


class TestStageRow:
    @staticmethod
    def _ladder():
        from stages.lib.page_classify.arbitration import classify_page

        return classify_page(None, None, frozenset())

    def test_three_levels_and_codeability_become_the_row(self):
        from stages.lib.page_classify.stage import _page_row

        row = _page_row(axes.gate(None, {}, CT_PAGE), self._ladder())
        assert (row["page_type"], row["page_subtype"]) == ("Lab Report/Result (Radiology)", "CT Scan Report")
        assert row["model_type"] == "Lab Report/Result > Radiology > CT Scan Report"
        assert (row["classification_category"], row["needs_review"]) == ("non_codeable", False)
        assert row["decided_by"] == "axes:canon/canon/canon"
        assert row["axes"]["l3"] == "CT Scan Report"

    def test_nothing_decided_is_unknown_for_review(self):
        from stages.lib.page_classify.stage import _page_row

        row = _page_row(axes.gate(None, {}, NO_TERMS), self._ladder())
        assert (row["page_type"], row["page_subtype"]) == ("Unknown", "Unknown")
        assert (row["classification_category"], row["needs_review"]) == ("non_codeable", True)

    def test_discharge_codeability_is_the_discharge_category(self):
        from stages.lib.page_classify.stage import _page_row

        result = axes.AxesResult(l1="Discharge Summary", l1_confidence=0.9, codeability="discharge")
        row = _page_row(result, self._ladder())
        assert (row["page_type"], row["page_subtype"]) == ("Discharge Summary", "Others")
        assert row["classification_category"] == "discharge_summary"

    def test_csv_axes_columns_are_codeable_csv_columns(self):
        from stages.lib.page_classify.stage import CODEABLE_COLS

        assert set(axes.AxesResult().csv_columns()) <= set(CODEABLE_COLS)


class TestStorage:
    def test_every_written_column_is_in_the_schema(self):
        from db import PAGE_CLASSIFICATION_FIELDS

        sql = (REPO / "schema" / "v1.sql").read_text(encoding="utf-8")
        table = re.search(r"CREATE TABLE page_classification \((.*?)\n\);", sql, re.S).group(1)
        columns = set(re.findall(r"^\s+([a-z_0-9]+)\s+[A-Z]", table, re.M))
        assert set(PAGE_CLASSIFICATION_FIELDS) <= columns
        assert "axes" in PAGE_CLASSIFICATION_FIELDS


class TestDownstreamNames:
    """Stages after classification read v24 names from the stored row."""

    ROW = {"page_type": "Progress Note", "page_subtype": "Progress Note",
           "axes": {"l1": "Progress Note", "canon_page_type": "Telephone Encounter", "title_l1": "Progress Note"}}

    def test_names_are_most_specific_first(self):
        assert axes.names_of(self.ROW) == ("Telephone Encounter", "Progress Note")
        assert axes.family_of(self.ROW) == "Progress Note"
        assert axes.title_of(self.ROW) == "Progress Note"

    def test_json_text_from_the_driver_reads_the_same(self):
        import json

        assert axes.names_of({**self.ROW, "axes": json.dumps(self.ROW["axes"])})[0] == "Telephone Encounter"

    def test_a_row_without_axes_falls_back_to_its_columns(self):
        assert axes.names_of({"page_type": "A", "page_subtype": "B"}) == ("B", "A")

    def test_encounter_tier_one_finds_the_visit_kind_under_progress_note(self):
        from stages.lib.encounter.encounter_classify import load_canon

        tier1 = load_canon().tier1
        assert next(n for n in axes.names_of(self.ROW) if n in tier1) == "Telephone Encounter"
        assert tier1["Telephone Encounter"] == "outpatient_tele"

    def test_every_encounter_tier_one_name_is_a_v24_name(self):
        from stages.lib.encounter.encounter_classify import load_canon

        assert set(load_canon().tier1) <= axes.v24_names()

    def test_default_dos_types_match_v24_names(self):
        from stages.lib.continuity.engine import uses_default_final_dos

        demographics = {"axes": {"l1": "Demographics", "l2": "Patient Demographics"}}
        assert uses_default_final_dos(*axes.names_of(demographics))
        assert not uses_default_final_dos(*axes.names_of(self.ROW))


class TestUnusableModelFolder:
    def test_git_lfs_pointer_is_not_a_model(self, tmp_path):
        from stages.lib.page_classify import axes

        (tmp_path / "encoder").mkdir()
        (tmp_path / "encoder" / "config.json").write_text("{}", encoding="utf-8")
        (tmp_path / "label_space.json").write_text("{}", encoding="utf-8")
        (tmp_path / "heads.pt").write_bytes(b"version https://git-lfs.github.com/spec/v1\noid sha256:ab\nsize 280789\n")
        reason = axes._unusable(tmp_path)
        assert reason and "git LFS pointer" in reason and "heads.pt" in reason

    def test_missing_files_are_named(self, tmp_path):
        from stages.lib.page_classify import axes

        assert "heads.pt" in (axes._unusable(tmp_path) or "")
