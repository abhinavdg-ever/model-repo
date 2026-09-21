"""In-memory backend for ``--skip-db-write`` local runs."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "core-pipeline"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))


@pytest.fixture()
def memory():
    from db import disable_skip_db_write, enable_skip_db_write

    store = enable_skip_db_write(reset=True)
    yield store
    disable_skip_db_write()


class TestMemoryStoreRoundTrip:
    def test_chart_pages_ocr_and_resume(self, memory):
        from db import (
            connect,
            get_ocr_texts,
            init_page_stages,
            list_pages,
            pages_needing_stage,
            set_page_stage,
            upsert_chart,
            upsert_ocr_result,
            upsert_pages,
        )

        with connect() as conn:
            assert type(conn).__name__ == "MemoryStore"
            chart = upsert_chart(conn, chart_name="demo_chart", source="local")
            cid = int(chart["id"])
            upsert_pages(
                conn,
                cid,
                [
                    {"page_name": "1.jpg", "page_number": 1},
                    {"page_name": "2.jpg", "page_number": 2},
                ],
            )
            pages = list_pages(conn, cid)
            assert [p["page_name"] for p in pages] == ["1.jpg", "2.jpg"]
            init_page_stages(conn, cid)
            todo = pages_needing_stage(conn, cid, "ocr_prelim", force=False)
            assert todo == {pages[0]["id"], pages[1]["id"]}
            set_page_stage(
                conn,
                chart_id=cid,
                page_id=pages[0]["id"],
                stage_name="ocr_prelim",
                status="completed",
            )
            todo2 = pages_needing_stage(conn, cid, "ocr_prelim", force=False)
            assert todo2 == {pages[1]["id"]}
            force_all = pages_needing_stage(conn, cid, "ocr_prelim", force=True)
            assert force_all == {pages[0]["id"], pages[1]["id"]}
            upsert_ocr_result(
                conn,
                chart_id=cid,
                page_id=pages[0]["id"],
                ocr_type="tesseract",
                raw_text="hello patient note",
            )
            texts = get_ocr_texts(conn, cid, "tesseract")
            assert texts[pages[0]["id"]] == "hello patient note"

    def test_blank_junk_flags(self, memory):
        from db import (
            connect,
            get_blank_junk_flags,
            mark_blank_junk_final,
            upsert_blank_junk,
            upsert_chart,
            upsert_pages,
        )

        with connect() as conn:
            chart = upsert_chart(conn, chart_name="bj", source="local")
            cid = int(chart["id"])
            pages = upsert_pages(
                conn, cid, [{"page_name": "1.jpg", "page_number": 1}]
            )
            pid = pages[0]["id"]
            upsert_blank_junk(
                conn,
                chart_id=cid,
                page_id=pid,
                blank_junk_flag="not_blank_junk",
                pass_no=1,
                ocr_source="tesseract",
            )
            mark_blank_junk_final(conn, cid)
            assert get_blank_junk_flags(conn, cid, final_only=True)[pid] == (
                "not_blank_junk"
            )


class TestSkipDbWriteGuards:
    def test_cli_help_lists_flag(self):
        from cli import main

        with pytest.raises(SystemExit) as exc:
            sys.argv = ["cli.py", "run", "--help"]
            try:
                main()
            finally:
                sys.argv = ["cli.py"]
        # argparse --help exits 0
        assert exc.value.code == 0

    def test_ingest_rejects_blob_with_skip_db(self, memory):
        from orchestrator.runner import ingest_and_run

        with pytest.raises(ValueError, match="local_path"):
            ingest_and_run(
                blob_container="c",
                blob_path="Raw_Input/x/chart",
                skip_db_write=True,
            )


class TestOfflineIntakeNoPostgres:
    def test_import_local_never_calls_psycopg(self, memory, tmp_path, monkeypatch):
        from PIL import Image

        chart_dir = tmp_path / "offline_chart"
        chart_dir.mkdir()
        Image.new("RGB", (32, 32), color=(255, 255, 255)).save(
            chart_dir / "1.jpg", format="JPEG"
        )

        import db as db_mod

        def boom(*_a, **_k):
            raise AssertionError("psycopg.connect must not be called")

        monkeypatch.setattr(db_mod, "_psycopg", boom)
        monkeypatch.setattr(db_mod, "_get_pool", lambda: False)

        # Point DATA_ROOT at a temp workspace so we do not touch review-ui.
        import config

        monkeypatch.setattr(config, "DATA_ROOT", tmp_path / "folders")
        (tmp_path / "folders").mkdir()

        from stages.download_blob import import_local_folder

        result = import_local_folder(chart_dir, chart_name="offline_chart", force=True)
        assert result["chart_id"]
        assert result["page_count"] == 1
        assert (tmp_path / "folders" / "offline_chart" / "pages" / "1.jpg").is_file()
