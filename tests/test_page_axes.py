"""Page axes: the inference copy of train_axes.py, and the gate that decides
each level by the model (its top 3), the keyword canon, or Others.

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

LAB = "Lab Report"
RAD = "Radiology Report"
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


def raw(l1=LAB, l1_p=0.9, second=0.05, l2=RAD, l2_p=0.9, l3=None, l3_p=None,
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

    def test_nothing_sure_is_others(self):
        result = gate(raw(l1="Administrative", l1_p=0.2, second=0.1), text=NO_TERMS)
        assert result.route == "review"
        assert (result.l1, result.l2, result.l3) == ("Others", None, None)
        assert result.sources[:3] == ("others", "-", "-")
        assert result.decided_by == "others"


class TestLevelTwoAndThree:
    def test_model_l2_at_fifty_percent_and_l3_from_the_canon(self):
        result = gate(raw(l2_p=0.51, l3="MRI Report", l3_p=0.99))
        assert (result.l2, result.sources[1]) == (RAD, "model")
        assert (result.l3, result.sources[2]) == ("CT Scan Report", "canon")
        assert result.l3_confidence >= 0.5
        assert result.prediction["L3"] == "MRI Report"  # the model's L3 is kept, not used

    def test_low_model_l2_falls_back_to_the_canon(self):
        result = gate(raw(l2="Lab Result", l2_p=0.45))
        assert (result.l2, result.sources[1]) == (RAD, "canon")

    def test_l3_needs_the_canon_to_be_sure(self):
        result = gate(raw(l3="CT Scan Report", l3_p=0.99), text="RADIOLOGY\nresults below\n")
        assert result.l2 == RAD
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
        assert (result.l1, result.l2, result.l3) == (LAB, RAD, "CT Scan Report")
        assert result.as_json()["sources"] == "canon/canon/canon/-/canon"
        assert result.as_json()["decided_by"] == "canon"
        assert result.as_json()["model_top3"] is None
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
        ((LAB, RAD, "CT Scan Report"), ("Lab Report (Radiology Report)", "CT Scan Report")),
        ((LAB, RAD, None), (LAB, RAD)),
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
    def test_three_levels_and_codeability_become_the_row(self):
        from stages.lib.page_classify.stage import _page_row

        row = _page_row(axes.gate(None, {}, CT_PAGE))
        assert (row["page_type"], row["page_subtype"]) == ("Lab Report (Radiology Report)", "CT Scan Report")
        assert row["model_type"] == "Lab Report > Radiology Report > CT Scan Report"
        assert (row["classification_category"], row["needs_review"]) == ("non_codeable", False)
        assert row["decided_by"] == "canon"
        assert row["axes"]["l3"] == "CT Scan Report"
        assert not any(key.startswith(("bert_", "keyword_")) for key in row)  # those columns stay empty

    def test_nothing_decided_is_others_for_review(self):
        from stages.lib.page_classify.stage import _page_row

        row = _page_row(axes.gate(None, {}, NO_TERMS))
        assert (row["page_type"], row["page_subtype"]) == ("Others", "Others")
        assert (row["classification_category"], row["needs_review"]) == ("non_codeable", True)
        assert row["decided_by"] == "others"

    def test_discharge_codeability_is_the_discharge_category(self):
        from stages.lib.page_classify.stage import _page_row

        result = axes.AxesResult(l1="Discharge Summary", l1_confidence=0.9, codeability="discharge")
        row = _page_row(result)
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
    """Stages after classification read the stored row's names."""

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

    def test_every_encounter_tier_one_name_is_a_page_name(self):
        from stages.lib.encounter.encounter_classify import load_canon

        assert set(load_canon().tier1) <= axes.names()

    def test_default_dos_types_match_page_names(self):
        from stages.lib.continuity.engine import uses_default_final_dos

        demographics = {"axes": {"l1": "Demographics", "l2": "Patient Information"}}
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


# --- The flow: the model's top 3, the canon, Others ------------------------------------

SLEEP_PAGE = """POLYSOMNOGRAPHY
Sleep study performed overnight. Apnea hypopnea index and oxygen saturation recorded.
"""


class TestModelTopThree:
    def test_the_top_three_paths_are_stored_with_their_scores(self):
        r = raw(l1_p=0.8, second=0.15, l2_p=0.9)
        r["L1_top"].append(("Administrative", 0.05))
        r["by_L1"] = {"Progress Note": {"L2": "CC/HPI", "L2_confidence": 0.5},
                      "Administrative": {"L2": "Superbill", "L2_confidence": 0.6}}
        top = axes.gate(r, SPACE, CT_PAGE).as_json()["model_top3"]
        assert [t["rank"] for t in top] == [1, 2, 3]
        assert top[0]["path"] == f"{LAB} > {RAD}" and top[0]["score"] == pytest.approx(0.72)
        assert top[1]["path"] == "Progress Note > CC/HPI" and top[1]["score"] == pytest.approx(0.075)
        assert top[2]["score"] == pytest.approx(0.03)

    def test_the_decoders_own_top3_is_used_as_given(self):
        r = raw()
        r["top3"] = [{"path": "OTHER", "L1": "OTHER", "L2": None, "L3": None, "L1_confidence": 0.6,
                      "L2_confidence": None, "L3_confidence": None, "score": 0.6}]
        top = axes.gate(r, SPACE, CT_PAGE).model_top3
        assert (top[0]["l1"], top[0]["folded"]) == ("Others", True)  # never the folded class name


class TestCanonFallback:
    def test_a_class_the_model_folded_is_named_by_the_canon(self):
        result = gate(raw(l1="OTHER", l1_p=0.9, l2=None), text=SLEEP_PAGE)
        assert (result.route, result.decided_by) == ("canon", "canon")
        assert (result.l1, result.l2, result.l3) == (LAB, "Diagnostic Study", "Sleep Study Report")
        assert result.model_top3[0]["l1"] == "Others"

    def test_an_l2_the_model_folded_to_others_lets_the_canon_name_it(self):
        result = gate(raw(l2="Others", l2_p=0.9))
        assert (result.l2, result.sources[1]) == (RAD, "canon")

    def test_others_from_the_model_stays_when_the_canon_has_nothing(self):
        result = gate(raw(l1="Administrative", l2="Others", l2_p=0.9), text=NO_TERMS)
        assert (result.l1, result.l2, result.sources[1]) == ("Administrative", "Others", "model")


# --- page_rules ----------------------------------------------------------------------

LAB_PAGE = """COMPREHENSIVE METABOLIC PANEL
Specimen: serum   Accession: A12345
Collected: 03/14/2024   Reported: 03/15/2024
Test        Result   Flag   Units   Reference Range
Sodium      139             mmol/L  135-145
Ordering Provider: Dr Example
"""


class TestRules:
    def test_lab_guard_turns_a_progress_note_lab_page_into_a_lab_report(self):
        result = gate(raw(l1="Progress Note", l2=None), text=LAB_PAGE)
        assert (result.l1, result.l2, result.rules) == (LAB, "Lab Result", ("lab_guard",))
        assert result.sources[:2] == ("rule", "rule")
        assert result.decided_by == "rule"

    def test_lab_guard_leaves_a_note_with_a_body_alone(self):
        note = "OFFICE VISIT\nChief Complaint: cough\nAssessment and Plan: rest\n" + LAB_PAGE
        result = gate(raw(l1="Progress Note", l2=None), text=note)
        assert (result.l1, result.rules) == ("Progress Note", ())

    def test_one_marker_is_not_enough_but_a_panel_title_is(self):
        one = "Patient reported feeling well.\nNo complaints today.\n"
        assert gate(raw(l1="Progress Note", l2=None), text=one).l1 == "Progress Note"
        panel = "CBC WITH DIFFERENTIAL FINAL REPORT\nWBC 6.1\nHGB 13.2\n"
        assert gate(raw(l1="Progress Note", l2=None), text=panel).l1 == LAB

    @pytest.mark.parametrize("title, path", [
        ("CONTINUITY OF CARE DOCUMENT", ("Clinical Summary", "Summary of Care (C-CDA)")),
        ("After Visit Summary", ("Clinical Summary", "After-Visit Summary")),
        ("Medical Records Request", ("Provider Letter", "Records Request")),
        ("SUPERBILL", ("Administrative", "Superbill")),
    ])
    def test_a_title_decides(self, title, path):
        text = f"{title}\nPatient: Test Person\nmade up body text\n"
        result = gate(raw(l1="Progress Note", l2=None), text=text)
        assert (result.l1, result.l2) == path
        assert result.codeability in ("non_codeable", "needs_review")

    def test_a_summary_mentioned_inside_a_note_is_not_a_title(self):
        text = "OFFICE VISIT\nChief Complaint: cough\nThe after visit summary was printed for the patient.\n"
        assert gate(raw(l1="Progress Note", l2=None), text=text).l1 == "Progress Note"

    def test_a_discharge_summary_page_keeps_its_path(self):
        text = "Summary of Care\nHospital course uneventful\n"
        result = gate(raw(l1="Discharge Summary", l2="Depart/Discharge Note"), text=text)
        assert result.l1 == "Discharge Summary"

    def test_an_admission_h_and_p_belongs_to_the_stay(self):
        text = "HISTORY AND PHYSICAL\nAdmission Date: 03/14/2024   Room/Bed: 4B-12\nChief Complaint: chest pain\n"
        result = gate(raw(l1="Progress Note", l2=None), text=text)
        assert (result.l1, result.l2, result.codeability) == ("Discharge Summary", "History and Physical", "discharge")

    def test_an_office_h_and_p_stays_a_progress_note(self):
        text = "HISTORY AND PHYSICAL\nNew patient visit\nChief Complaint: chest pain\n"
        assert gate(raw(l1="Progress Note", l2=None), text=text).l1 == "Progress Note"


# --- staging/page_classification.json ------------------------------------------------


class TestStaging:
    def test_written_atomically_and_read_back_by_page_name(self, tmp_path, monkeypatch):
        from stages.lib.page_classify import stage

        monkeypatch.setattr(stage, "staging_dir", lambda chart: tmp_path / chart / "staging")
        result = axes.gate(raw(), SPACE, CT_PAGE)
        row = stage._page_row(result)
        entry = stage._staged_entry(row, result, "final1")
        path = stage.write_staged("chart1", {"chart1_Pg1": entry})
        assert path == tmp_path / "chart1" / "staging" / "page_classification.json"
        assert not path.with_suffix(".tmp").exists()
        pages = stage.read_staged("chart1")
        assert pages["chart1_Pg1"]["decided_by"] == "model"
        assert pages["chart1_Pg1"]["page_type"] == "Lab Report (Radiology Report)"
        assert pages["chart1_Pg1"]["model_top3"][0]["path"] == f"{LAB} > {RAD}"
        assert pages["chart1_Pg1"]["axes"]["l3"] == "CT Scan Report"

    def test_a_missing_or_foreign_file_reads_empty(self, tmp_path, monkeypatch):
        from stages.lib.page_classify import stage

        monkeypatch.setattr(stage, "staging_dir", lambda chart: tmp_path / chart / "staging")
        assert stage.read_staged("nothing") == {}
        (tmp_path / "old" / "staging").mkdir(parents=True)
        (tmp_path / "old" / "staging" / "page_classification.json").write_text('{"version": 0}', encoding="utf-8")
        assert stage.read_staged("old") == {}


# --- the converted canon ------------------------------------------------------------


class TestConvertedCanon:
    @staticmethod
    def _files():
        import json

        folder = CORE / "stages" / "lib" / "keyword-canon"
        return (json.loads((folder / "page_canon.json").read_text(encoding="utf-8")),
                json.loads((folder / "page_taxonomy.json").read_text(encoding="utf-8")))

    def test_every_canon_path_is_in_the_taxonomy(self):
        canon, taxonomy = self._files()
        space = taxonomy["label_space"]
        for entry in canon["page_types"]:
            path = entry["path"]
            assert path["L1"] in space["L1"], entry["page_type"]
            if path["L2"]:
                assert path["L2"] in space["L2"][path["L1"]], entry["page_type"]
            if path["L3"]:
                assert path["L3"] in space["L3"][f"{path['L1']} > {path['L2']}"], entry["page_type"]

    def test_converting_again_changes_nothing(self):
        from stages.lib.page_classify.canon_convert import convert

        canon, taxonomy = self._files()
        again, unresolved = convert(canon, taxonomy)
        assert again == canon and unresolved == {}

    @pytest.mark.parametrize("old, new", [
        ({"L1": "Lab Report/Result", "L2": "Laboratory", "L3": "INR and Prothrombin Time"},
         {"L1": "Lab Report", "L2": "Lab Result", "L3": "Coagulation Report"}),
        ({"L1": "Lab Report/Result", "L2": "Diagnostic Study", "L3": "Myocardial Perfusion Study"},
         {"L1": "Lab Report", "L2": "Diagnostic Study", "L3": "Stress Test"}),
        ({"L1": "Consent Form", "L2": "HIPAA Form", "L3": ""},
         {"L1": "Consent and Authorization", "L2": "HIPAA and Privacy", "L3": ""}),
        ({"L1": "Demographics", "L2": "Registration Form", "L3": ""},
         {"L1": "Administrative", "L2": "Patient Registration", "L3": ""}),
        ({"L1": "Patient Communication", "L2": "Provider Letter", "L3": ""},
         {"L1": "Provider Letter", "L2": "Others", "L3": ""}),
    ])
    def test_old_paths_convert_by_the_tables(self, old, new):
        from stages.lib.page_classify.canon_convert import convert_entry

        _, taxonomy = self._files()
        entry = {"page_type": "Made Up Page Type", "family": "", "path": old, "specialty": ""}
        converted, unresolved = convert_entry(entry, taxonomy)
        assert converted["path"] == new and unresolved == []

    def test_an_unplaceable_l2_is_reported_not_guessed_silently(self):
        from stages.lib.page_classify.canon_convert import convert_entry

        _, taxonomy = self._files()
        entry = {"page_type": "Made Up Page Type", "family": "",
                 "path": {"L1": "Administrative", "L2": "Administrative Record", "L3": ""}, "specialty": ""}
        converted, unresolved = convert_entry(entry, taxonomy)
        assert converted["path"]["L2"] == "Others" and unresolved
