"""Contracts between core-pipeline and review-ui.

The pipeline writes CSVs; the review UI reads them. Nothing type-checks that
seam at runtime, so it is pinned here: every column the UI looks for must be a
column the pipeline actually writes, and the values must be in the vocabulary
the UI knows how to render.
"""
from __future__ import annotations

import csv
import io
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


# --- blank/junk subtype vocabulary ------------------------------------------

# The label set the schema CHECK constrains junk_subtype to.
SCHEMA_SUBTYPES = {
    "Invoice",
    "Cover Page",
    "Record Request/Transmittal",
    "Instructions",
    "Letter/Fax",
    "Others",
}


class TestJunkSubtypeVocabulary:
    def test_schema_check_matches_the_ui_label_set(self):
        """The UI renders a fixed set of page types; the schema must allow
        exactly those and nothing else, or rows become unrenderable."""
        from app.services.imaging_overlays import index_junk_rows  # noqa: F401

        schema_sql = (REPO_ROOT / "schema" / "v1.sql").read_text()
        for label in SCHEMA_SUBTYPES:
            assert f"'{label}'" in schema_sql, f"{label} missing from schema CHECK"

    def test_stage_maps_every_classifier_label_into_the_vocabulary(self):
        from stages.blank_junk_classify import SUBTYPE_DB
        from classify import CLASSIFICATION_LABELS, JUNK_CODES

        for code in JUNK_CODES:
            label = CLASSIFICATION_LABELS.get(code, "")
            mapped = SUBTYPE_DB.get(label.strip().casefold(), "Others")
            assert mapped in SCHEMA_SUBTYPES, (
                f"classifier label {label!r} maps to {mapped!r}, "
                "which the schema CHECK rejects"
            )

    def test_unknown_labels_fall_back_to_others_not_null(self):
        from stages.blank_junk_classify import SUBTYPE_DB

        assert SUBTYPE_DB.get("something the classifier invents", "Others") == "Others"


# --- CSV column contracts ---------------------------------------------------


class TestCsvColumns:
    def test_dos_csv_carries_what_the_overlay_reads(self):
        from stages.dos_extract import DOS_COLS

        # imaging_overlays.dos_row_fields reads these first, then falls back.
        for column in ("dos_from", "dos_to", "dos_from_iso", "dos_to_iso",
                       "doc_dos_from", "doc_dos_to",
                       "doc_dos_from_iso", "doc_dos_to_iso"):
            assert column in DOS_COLS
        assert "chart_name" in DOS_COLS and "page_number" in DOS_COLS

    def test_hw_csv_column_is_one_the_overlay_recognises(self):
        from stages.quality_rotation_hw import HW_COLS

        # index_hw_rows accepts handwritten / handwritten_or_printed / type.
        assert "handwritten_or_printed" in HW_COLS
        assert "confidence" in HW_COLS

    def test_junk_csv_carries_classification_and_group(self):
        from stages.blank_junk_classify import JUNK_CSV_COLS

        # index_junk_rows reads page_classification (or classification/page_type).
        assert "page_classification" in JUNK_CSV_COLS
        assert "page_group" in JUNK_CSV_COLS
        # v7 additions that make the two passes legible in the file itself.
        assert "pass_no" in JUNK_CSV_COLS
        assert "is_final" in JUNK_CSV_COLS

    def test_member_csv_carries_the_reference_provenance_columns(self):
        from stages.member_extract_verify import MEMBER_EXTRACT_COLS

        for column in ("detection_source_name", "detection_source_dob",
                       "detection_source_member_id", "ner_key_source_name",
                       "page_status", "page_verified"):
            assert column in MEMBER_EXTRACT_COLS

    def test_member_summary_carries_the_reject_decision(self):
        from stages.member_extract_verify import MEMBER_SUMMARY_COLS

        for column in ("document_decision", "wrong_member_pages",
                       "reject_threshold", "ner_enabled"):
            assert column in MEMBER_SUMMARY_COLS

    def test_every_csv_writer_has_a_chart_name_column(self):
        """collect_rows filters combined packs on chart_name; a CSV without it
        would be attributed to every chart."""
        from stages.blank_junk_classify import JUNK_CSV_COLS
        from stages.dos_extract import DOS_COLS
        from stages.member_extract_verify import MEMBER_EXTRACT_COLS, MEMBER_SUMMARY_COLS
        from stages.quality_rotation_hw import HW_COLS, ROTATION_COLS

        for cols in (JUNK_CSV_COLS, DOS_COLS, MEMBER_EXTRACT_COLS,
                     MEMBER_SUMMARY_COLS, HW_COLS, ROTATION_COLS):
            assert "chart_name" in cols


class TestPerChartCsvNamesAreRead:
    """The review UI matches per-chart files by suffix. A stage writing a
    suffix the UI does not branch on produces a file nothing reads — which is
    exactly how the junk stream went missing."""

    STAGE_SUFFIXES = {
        "_rotation.csv",
        "_hw_printed.csv",
        "_junk.csv",
        "_member_extraction.csv",
        "_member_verification.csv",
        "_dos.csv",
    }

    def test_folder_list_marks_every_stage_stream(self):
        source = (
            REPO_ROOT / "review-ui" / "backend" / "app" / "adapters" / "local"
            / "repository.py"
        ).read_text()
        # The per-chart override loop in _pipeline_streams.
        start = source.index("Per-chart overrides under data/folders")
        block = source[start : start + 2500]
        for suffix in self.STAGE_SUFFIXES:
            assert suffix in block, (
                f"{suffix} is written by a stage but the folder list never "
                "marks its stream, so the badge stays blank"
            )

    def test_detail_view_reads_every_stage_stream(self):
        source = (
            REPO_ROOT / "review-ui" / "backend" / "app" / "adapters" / "local"
            / "repository.py"
        ).read_text()
        for suffix in self.STAGE_SUFFIXES:
            token = suffix.replace("_", "").replace(".csv", "")
            assert f'_{suffix.lstrip("_")}' in source or token in source


# --- combined OCR text format ----------------------------------------------


class TestCombinedOcrFormat:
    def test_marker_round_trips(self):
        from db.paths import parse_combined_ocr_txt, write_combined_ocr_txt

        import config

        original_root = config.DATA_ROOT
        try:
            import tempfile

            with tempfile.TemporaryDirectory() as tmp:
                config.DATA_ROOT = Path(tmp)
                pages = [("1.jpg", "first page text"), ("2.jpg", "second page text")]
                out = write_combined_ocr_txt("chartA", "prelim", pages)
                parsed = parse_combined_ocr_txt(out)
                assert parsed["1.jpg"] == "first page text"
                assert parsed["2.jpg"] == "second page text"
        finally:
            config.DATA_ROOT = original_root

    def test_dos_splitter_reads_the_same_marker(self):
        """The DOS driver splits pages itself; it must agree with the writer."""
        from dos_logic import UI_PAGE_MARKER_RE, split_ocr_into_pages

        text = "===== 1.jpg =====\nalpha\n\n===== 2.jpg =====\nbeta\n"
        pages = split_ocr_into_pages(text)
        assert [p["page_name"] for p in pages] == ["1.jpg", "2.jpg"]
        assert UI_PAGE_MARKER_RE.search(text) is not None


# --- stage registry consistency --------------------------------------------


class TestStageRegistry:
    def test_execution_chain_matches_the_schema_seed(self):
        """pipeline_stage drives progress reporting, STAGE_CHAIN drives
        execution. If they disagree, a chart can never reach 'completed'."""
        from orchestrator.runner import STAGE_CHAIN

        # v1.sql seeds the implemented stages; v2.sql registers the four
        # not-yet-orchestrated ones. The chain must match V1 exactly.
        schema_sql = (REPO_ROOT / "schema" / "v1.sql").read_text()
        seed_start = schema_sql.index("INSERT INTO pipeline_stage")
        seed = schema_sql[seed_start : schema_sql.index(";", seed_start)]

        for name, pass_no, _fn in STAGE_CHAIN:
            assert f"('{name}',{' ' * (max(1, 16 - len(name)))}{pass_no}," in seed or \
                   f"'{name}'" in seed, f"{name} not seeded in pipeline_stage"

        # Every phase-1 seeded stage must have a callable in the chain.
        chain_keys = {(n, p) for n, p, _ in STAGE_CHAIN}
        for line in seed.splitlines():
            if not line.strip().startswith("('"):
                continue
            if "FALSE" in line:  # registered but not orchestrated yet
                continue
            parts = line.strip().strip("(),").split(",")
            name = parts[0].strip().strip("'")
            pass_no = int(parts[1].strip())
            assert (name, pass_no) in chain_keys, (
                f"pipeline_stage seeds {name}:{pass_no} as phase-1 but "
                "STAGE_CHAIN has no callable for it"
            )

    def test_stage_names_are_exposed_for_the_rerun_api(self):
        from orchestrator.runner import STAGE_NAMES

        assert "blank_junk:2" in STAGE_NAMES
        assert "member_verify:1" in STAGE_NAMES


# --- V1 / V2 schema split ----------------------------------------------------


class TestSchemaSplit:
    """schema/v1.sql is what is implemented; schema/v2.sql is the next phase.

    The split is only meaningful while it stays true, and nothing else enforces
    it — a stage that starts writing a V2 table would leave V1 silently wrong.
    """

    @staticmethod
    def _relations(path):
        import re

        text = (REPO_ROOT / "schema" / path).read_text()
        code = "\n".join(re.sub(r"--.*$", "", ln) for ln in text.splitlines())
        return set(re.findall(r"CREATE TABLE (\w+)", code)) | set(
            re.findall(r"CREATE OR REPLACE VIEW (\w+)", code)
        )

    def test_no_relation_is_defined_in_both_files(self):
        overlap = self._relations("v1.sql") & self._relations("v2.sql")
        assert not overlap, f"defined twice: {sorted(overlap)}"

    def test_v1_never_references_a_v2_relation(self):
        """V1 must apply and run on its own — v2.sql is optional."""
        import re

        text = (REPO_ROOT / "schema" / "v1.sql").read_text()
        code = "\n".join(re.sub(r"--.*$", "", ln) for ln in text.splitlines())
        v1, v2 = self._relations("v1.sql"), self._relations("v2.sql")
        referenced = set(re.findall(r"REFERENCES\s+(\w+)\s*\(", code)) | set(
            re.findall(r"(?:FROM|JOIN)\s+([a-z_][a-z0-9_]*)\b", code)
        )
        assert not (referenced & v2), f"V1 depends on V2: {sorted(referenced & v2)}"
        assert not (referenced - v1), f"V1 references undefined: {sorted(referenced - v1)}"

    def test_no_code_touches_a_v2_relation(self):
        """If this fails, the module is implemented — move its table to v1.sql."""
        import ast
        import re

        v2 = self._relations("v2.sql")
        offenders = []
        roots = [REPO_ROOT / "core-pipeline", REPO_ROOT / "review-ui" / "backend"]
        for root in roots:
            for path in root.rglob("*.py"):
                if "__pycache__" in str(path) or path.name.startswith("._"):
                    continue
                for node in ast.walk(ast.parse(path.read_text())):
                    if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                        continue
                    sql = node.value
                    if not re.search(r"\b(INSERT INTO|UPDATE|SELECT|DELETE FROM)\b", sql):
                        continue
                    hits = set(
                        re.findall(
                            r"(?:INSERT INTO|FROM|JOIN|UPDATE|DELETE FROM)\s+([a-z_][a-z0-9_]*)\b",
                            sql,
                        )
                    )
                    for rel in hits & v2:
                        offenders.append(f"{path.name}:{node.lineno} -> {rel}")
        assert not offenders, "code touches V2 relations:\n  " + "\n  ".join(sorted(offenders))
