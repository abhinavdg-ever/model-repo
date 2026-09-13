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



# --- shared source-file walker ----------------------------------------------


# Directories that contain code we did not write. A virtualenv is the dangerous
# one: docs/API.md tells you to create core-pipeline/.venv, so on any machine
# that followed the setup, rglob("*.py") walks into site-packages and tries to
# parse third-party sources — several of which carry a UTF-8 BOM and are not
# valid input to ast.parse without it being stripped.
_SKIP_DIRS = {
    "__pycache__", ".git", "node_modules", "site-packages", "dist", "build",
    ".mypy_cache", ".pytest_cache", ".tox", ".eggs",
}


def _is_venv(directory):
    """A virtualenv is identified by pyvenv.cfg, whatever it is named."""
    return (directory / "pyvenv.cfg").is_file()


def repo_python_files(*roots):
    """Every .py file we actually own under `roots`.

    Walks manually rather than using rglob so an excluded directory is pruned
    instead of merely filtered — descending into site-packages is slow even
    when every result is discarded.
    """
    for root in roots:
        stack = [Path(root)]
        while stack:
            directory = stack.pop()
            try:
                entries = list(directory.iterdir())
            except (OSError, PermissionError):
                continue
            if any(e.name == "pyvenv.cfg" for e in entries):
                continue
            for entry in entries:
                if entry.is_dir():
                    if entry.name in _SKIP_DIRS or entry.name.startswith("."):
                        continue
                    stack.append(entry)
                elif entry.suffix == ".py" and not entry.name.startswith("._"):
                    yield entry


class TestJunkSubtypeVocabulary:
    def test_schema_check_matches_the_ui_label_set(self):
        """The UI renders a fixed set of page types; the schema must allow
        exactly those and nothing else, or rows become unrenderable."""
        from app.services.imaging_overlays import index_junk_rows  # noqa: F401

        schema_sql = (REPO_ROOT / "schema" / "v1.sql").read_text(encoding="utf-8")
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
        ).read_text(encoding="utf-8")
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
        ).read_text(encoding="utf-8")
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
        schema_sql = (REPO_ROOT / "schema" / "v1.sql").read_text(encoding="utf-8")
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

        text = (REPO_ROOT / "schema" / path).read_text(encoding="utf-8")
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

        text = (REPO_ROOT / "schema" / "v1.sql").read_text(encoding="utf-8")
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
        for path in repo_python_files(
            REPO_ROOT / "core-pipeline", REPO_ROOT / "review-ui" / "backend"
        ):
            if True:
                # utf-8-sig so a BOM is stripped rather than reaching ast.parse
                for node in ast.walk(
                    ast.parse(path.read_text(encoding="utf-8-sig"))
                ):
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


# --- cross-platform text IO --------------------------------------------------


class TestTextIoDeclaresEncoding:
    """Every text read/write must name its encoding.

    Python defaults to the *locale* encoding: UTF-8 on macOS/Linux, cp1252 on
    Windows. The repo's sources are UTF-8 and full of em-dashes, so a call that
    omits the encoding works everywhere the author tested and then dies on
    Windows with a UnicodeDecodeError about the 'charmap' codec.

    Checked via AST, not regex, so prose in docstrings and comments cannot
    trigger it — and neither can this docstring.
    """

    @staticmethod
    def _calls():
        """Yield (path, lineno, func_name, has_encoding_kwarg, args) per call."""
        import ast

        for path in repo_python_files(
            REPO_ROOT / "core-pipeline",
            REPO_ROOT / "review-ui" / "backend",
            REPO_ROOT / "tests",
        ):
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if isinstance(func, ast.Attribute):
                    name = func.attr
                    # PIL's Image.open is binary and takes no encoding.
                    owner = getattr(func.value, "id", None) or getattr(
                        getattr(func.value, "attr", None), "__str__", lambda: ""
                    )()
                    if name == "open" and owner == "Image":
                        continue
                elif isinstance(func, ast.Name):
                    name = func.id
                else:
                    continue
                has_enc = any(k.arg == "encoding" for k in node.keywords)
                yield path, node.lineno, name, has_enc, node.args

    def test_read_text_and_write_text_declare_encoding(self):
        offenders = [
            f"{p.relative_to(REPO_ROOT)}:{ln} .{name}()"
            for p, ln, name, has_enc, _ in self._calls()
            if name in {"read_text", "write_text"} and not has_enc
        ]
        assert not offenders, (
            "text IO without encoding= (breaks on Windows cp1252):\n  "
            + "\n  ".join(offenders)
        )

    def test_open_in_text_mode_declares_encoding(self):
        import ast

        offenders = []
        for p, ln, name, has_enc, args in self._calls():
            if name != "open" or has_enc:
                continue
            # A binary mode carries no encoding, so those are fine.
            mode = next(
                (a.value for a in args if isinstance(a, ast.Constant)
                 and isinstance(a.value, str)),
                "r",
            )
            if "b" in mode:
                continue
            offenders.append(f"{p.relative_to(REPO_ROOT)}:{ln} open()")
        assert not offenders, (
            "open() in text mode without encoding= (breaks on Windows cp1252):\n  "
            + "\n  ".join(offenders)
        )


# --- database URL scheme -----------------------------------------------------


class TestDatabaseUrlScheme:
    """Both services must accept the same DATABASE_URL value.

    They share the variable name but talk to psycopg directly, which rejects
    SQLAlchemy's "+psycopg" dialect suffix with an error that never mentions the
    scheme:

        missing "=" after "postgresql+psycopg://..." in connection info string

    review-ui always normalised it; core-pipeline did not, so a value copied
    from one .env to the other silently failed to connect.
    """

    FORMS = [
        "postgresql://u:p@h:5432/d",
        "postgresql+psycopg://u:p@h:5432/d",
        "postgres+psycopg://u:p@h:5432/d",
        "postgresql+psycopg2://u:p@h:5432/d",
    ]

    def test_core_pipeline_accepts_every_form(self):
        from config import _psycopg_url

        for form in self.FORMS:
            assert _psycopg_url(form) == "postgresql://u:p@h:5432/d", form

    def test_review_ui_accepts_every_form(self):
        from app.adapters.postgres.repository import _psycopg_url

        for form in self.FORMS[:3]:  # review-ui does not claim psycopg2
            assert _psycopg_url(form) == "postgresql://u:p@h:5432/d", form

    def test_both_env_examples_use_the_same_scheme(self):
        """A value copied between the two .env files must work in both."""
        import re

        schemes = {}
        for name in ("core-pipeline/.env.example", "review-ui/.env.example"):
            text = (REPO_ROOT / name).read_text(encoding="utf-8")
            m = re.search(r"^DATABASE_URL=([a-z0-9+]+)://", text, re.M)
            assert m, f"{name} has no DATABASE_URL line"
            schemes[name] = m.group(1)
        assert len(set(schemes.values())) == 1, (
            "the two .env.example files disagree, so copying between them "
            f"breaks: {schemes}"
        )


# --- batch intake discovery --------------------------------------------------


class TestBatchFolderDiscovery:
    """What counts as a chart folder in a drop directory.

    Getting this wrong is expensive in both directions: a missed folder is a
    chart that silently never runs, and a spurious one is an ingest that fails
    on "no pages" halfway through an unattended batch.
    """

    @staticmethod
    def _drop(tmp_path):
        from PIL import Image

        def img(path):
            path.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (4, 4), "white").save(path)

        img(tmp_path / "chart_a" / "p1.jpg")
        img(tmp_path / "chart_b" / "p1.png")
        img(tmp_path / ".hidden" / "p1.jpg")          # dotfolder: skipped
        (tmp_path / "no_images").mkdir()
        (tmp_path / "no_images" / "notes.txt").write_text("x", encoding="utf-8")
        (tmp_path / "loose.txt").write_text("x", encoding="utf-8")
        (tmp_path / "chart_a" / "._p1.jpg").write_text("", encoding="utf-8")
        return tmp_path

    def test_finds_only_subfolders_containing_images(self, tmp_path):
        from jobs.batch_intake import find_local_chart_folders

        names = [p.name for p in find_local_chart_folders(self._drop(tmp_path))]
        assert names == ["chart_a", "chart_b"], names

    def test_a_single_chart_folder_is_itself_the_chart(self, tmp_path):
        """Pointing at one chart must work, not return its subfolders."""
        from jobs.batch_intake import find_local_chart_folders

        drop = self._drop(tmp_path)
        found = find_local_chart_folders(drop / "chart_a")
        assert [p.name for p in found] == ["chart_a"]

    def test_appledouble_stubs_do_not_make_a_folder_a_chart(self, tmp_path):
        """A folder holding only ._ stubs has no real images."""
        from jobs.batch_intake import find_local_chart_folders

        (tmp_path / "stubs_only").mkdir()
        (tmp_path / "stubs_only" / "._p1.jpg").write_text("", encoding="utf-8")
        assert find_local_chart_folders(tmp_path) == []

    def test_missing_directory_is_an_error_not_an_empty_batch(self, tmp_path):
        from jobs.batch_intake import find_local_chart_folders

        with pytest.raises(RuntimeError, match="Not a directory"):
            find_local_chart_folders(tmp_path / "nope")

    def test_batch_rejects_both_sources_at_once(self):
        from jobs.batch_intake import run_batch

        with pytest.raises(ValueError, match="either local_root"):
            run_batch(local_root="/x", blob_container="c", blob_prefix="p")

    def test_batch_rejects_neither_source(self):
        from jobs.batch_intake import run_batch

        with pytest.raises(ValueError, match="either local_root"):
            run_batch()


# --- ingest request shape ----------------------------------------------------


class TestIngestAcceptsBothModes:
    """POST /api/charts/ingest takes blob_container+blob_path OR local_path.

    The mode check must run BEFORE the database probe: a malformed body is a 400
    whatever the database is doing, and reporting "database unavailable" for a
    request that was never valid sends the caller after the wrong problem.
    """

    @staticmethod
    def _client(monkeypatch):
        from fastapi.testclient import TestClient

        import api.main as main

        # Pretend the database is fine, so only the shape checks can fail.
        monkeypatch.setattr(main, "_require_db", lambda: None)
        return TestClient(main.app, raise_server_exceptions=False), main

    def test_both_sources_at_once_is_rejected(self, monkeypatch):
        client, _ = self._client(monkeypatch)
        r = client.post(
            "/api/charts/ingest",
            json={"blob_container": "c", "blob_path": "p", "local_path": "/x"},
        )
        assert r.status_code == 400
        assert "not both" in r.json()["detail"]

    def test_neither_source_is_rejected(self, monkeypatch):
        client, _ = self._client(monkeypatch)
        r = client.post("/api/charts/ingest", json={})
        assert r.status_code == 400
        assert "local_path" in r.json()["detail"]

    def test_half_a_blob_pair_is_rejected(self, monkeypatch):
        client, _ = self._client(monkeypatch)
        r = client.post("/api/charts/ingest", json={"blob_path": "p"})
        assert r.status_code == 400
        assert "together" in r.json()["detail"]

    def test_blob_mode_is_accepted(self, monkeypatch):
        client, main = self._client(monkeypatch)
        monkeypatch.setattr(main, "_bg_ingest", lambda payload: None)
        r = client.post(
            "/api/charts/ingest",
            json={"blob_container": "c", "blob_path": "run1/chart_x"},
        )
        assert r.status_code == 202, r.text
        assert r.json()["mode"] == "blob"
        assert r.json()["chart_name"] == "chart_x"

    def test_the_removed_import_local_endpoint_is_gone(self, monkeypatch):
        """Folded into ingest; two endpoints for one job is how they drift."""
        client, _ = self._client(monkeypatch)
        r = client.post("/api/charts/import-local", json={"source_path": "/x"})
        assert r.status_code in (404, 405)


# --- operator-facing behaviour -----------------------------------------------


class TestNerFallsBackInsteadOfCrashing:
    """A rules-only run must actually run, not raise ModelLoadError.

    The stage logs "NER layer INACTIVE — rules-only" from ner_status()["ready"],
    but the model_id it passed to the engine was gated on MEMBER_NER_ENABLED
    alone. With the flag true and checkpoints absent it announced rules-only and
    then called NER anyway, killing the chart after five completed stages.
    """

    def test_model_id_is_gated_on_ready_not_on_the_flag(self):
        src = (
            REPO_ROOT / "core-pipeline" / "stages" / "member_extract_verify.py"
        ).read_text(encoding="utf-8")
        assert "ner_model_id = MEMBER_NER_MODEL_ID if ner[\"ready\"] else None" in src
        assert "model_id=ner_model_id," in src
        assert "MEMBER_NER_MODEL_ID if MEMBER_NER_ENABLED else None" not in src

    def test_no_stale_downloader_command_in_any_message(self):
        """The old path stopped existing when Reference/ was deleted."""
        import re

        offenders = []
        for path in repo_python_files(REPO_ROOT / "core-pipeline"):
            text = path.read_text(encoding="utf-8")
            if "Member_Verification.Models.model_downloader" in text:
                offenders.append(str(path.relative_to(REPO_ROOT)))
        assert not offenders, f"stale downloader command in: {offenders}"


class TestStageLabels:
    """Every orchestrated stage has a display name, so logs read uniformly."""

    def test_every_stage_in_the_chain_has_a_label(self):
        from orchestrator.runner import STAGE_CHAIN
        from stages._support import STAGE_LABELS, stage_label

        for name, pass_no, _fn in STAGE_CHAIN:
            assert name in STAGE_LABELS, f"{name} has no display label"
            assert stage_label(name, pass_no)

    def test_a_second_pass_is_distinguishable(self):
        from stages._support import stage_label

        assert stage_label("blank_junk", 1) == "Blank/Junk"
        assert stage_label("blank_junk", 2) == "Blank/Junk pass 2"


class TestChartResetKeepsTheAuditTrail:
    """A re-run replaces results but must not erase the run log."""

    def test_pipeline_jobs_is_not_wiped(self):
        from db import CHART_RESULT_TABLES

        assert "pipeline_jobs" not in CHART_RESULT_TABLES, (
            "pipeline_jobs is the audit trail — the point of a re-run is being "
            "able to compare it against the previous attempt"
        )

    def test_the_client_roster_is_not_wiped(self):
        from db import CHART_RESULT_TABLES

        assert "manifest_member_list" not in CHART_RESULT_TABLES, (
            "the manifest is the client's data, not our output"
        )

    def test_every_wiped_table_actually_has_a_chart_id(self):
        """A DELETE ... WHERE chart_id on a table without one is a runtime error."""
        import re

        from db import CHART_RESULT_TABLES

        schema = (REPO_ROOT / "schema" / "v1.sql").read_text(encoding="utf-8")
        code = "\n".join(re.sub(r"--.*$", "", ln) for ln in schema.splitlines())
        for table in CHART_RESULT_TABLES:
            m = re.search(rf"CREATE TABLE {table}\s*\((.*?)\n\);", code, re.S)
            assert m, f"{table} is not in v1.sql"
            assert "chart_id" in m.group(1), f"{table} has no chart_id column"
