"""Batch / intake resume behaviour (force=false)."""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "core-pipeline"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))


def test_blob_resume_does_not_wipe_when_force_false():
    """force=false must not clear workspace just because pages already exist."""
    from stages.utilities import download_blob

    src = inspect.getsource(download_blob.run_download)
    # Wipe only under force=True — not `force or had_local`.
    assert "if force or had_local" not in src
    assert "if force:" in src
    assert "Blob resume" in src or "force=false" in src


def test_local_resume_keeps_existing_pages():
    from stages.utilities import download_blob

    src = inspect.getsource(download_blob.import_local_folder)
    # Existing workspace pages are reused, never re-copied from source.
    assert "existing = list_local_pages(name)" in src
    assert "register_local_pages(" in src
    # force resets DB results only; page images go only on redownload_pages.
    assert "reset_chart_results(" in src
    assert "if redownload_pages:" in src
    assert "clear_page_image_dirs(" in src


def test_chart_is_pipeline_complete_logic():
    from db.chart_status import compute_progress

    pages = 2
    # One incomplete stage → not complete
    rows = [
        {
            "stage_name": "ocr_quality",
            "pass_no": 1,
            "seq": 10,
            "label": "Q",
            "completed": 2,
            "skipped": 0,
            "failed": 0,
            "pending": 0,
            "processing": 0,
        },
        {
            "stage_name": "section_headers",
            "pass_no": 1,
            "seq": 55,
            "label": "H",
            "completed": 0,
            "skipped": 0,
            "failed": 0,
            "pending": 2,
            "processing": 0,
        },
    ]
    prog = compute_progress(rows, pages_total=pages)
    assert prog["status"] == "processing"
    assert prog["current_stage"] == "section_headers"

    rows[1]["completed"] = 2
    rows[1]["pending"] = 0
    prog2 = compute_progress(rows, pages_total=pages)
    assert prog2["current_stage"] is None
    assert prog2["status"] == "completed"


def test_batch_summarise_counts_skipped():
    from jobs.batch_intake import _summarise

    summary = _summarise(
        [
            {"status": "completed"},
            {"status": "skipped", "skip_reason": "already_complete"},
            {"status": "failed"},
        ],
        started=0.0,
    )
    assert summary["completed"] == 1
    assert summary["skipped"] == 1
    assert summary["failed"] == 1


def test_select_batch_sources_skips_complete_when_enough_incomplete(monkeypatch):
    from jobs import batch_intake

    sources = [
        ("/a", "chart_a", "local"),
        ("/b", "chart_b", "local"),
        ("/c", "chart_c", "local"),
        ("/d", "chart_d", "local"),
    ]
    monkeypatch.setattr(
        batch_intake,
        "_chart_names_pipeline_complete",
        lambda names: {"chart_a", "chart_b"},
    )
    picked = batch_intake.select_batch_sources(sources, 2)
    assert [n for _s, n, _m in picked] == ["chart_c", "chart_d"]


def test_select_batch_sources_fills_with_complete_when_needed(monkeypatch):
    from jobs import batch_intake

    sources = [
        ("/a", "chart_a", "local"),
        ("/b", "chart_b", "local"),
        ("/c", "chart_c", "local"),
    ]
    monkeypatch.setattr(
        batch_intake,
        "_chart_names_pipeline_complete",
        lambda names: {"chart_a", "chart_b"},
    )
    picked = batch_intake.select_batch_sources(sources, 2)
    assert [n for _s, n, _m in picked] == ["chart_c", "chart_a"]


def test_select_batch_sources_keeps_all_when_drop_fits():
    from jobs.batch_intake import select_batch_sources

    sources = [
        ("/a", "chart_a", "local"),
        ("/b", "chart_b", "local"),
    ]
    # Even if both were complete, the drop fits in sample=2 — keep both.
    picked = select_batch_sources(sources, 2, prefer_incomplete=True)
    assert picked == sources


def test_select_batch_sources_no_sample_returns_all():
    from jobs.batch_intake import select_batch_sources

    sources = [("/a", "a", "local"), ("/b", "b", "local")]
    assert select_batch_sources(sources, None) == sources


def test_filter_sources_by_chart_names():
    from jobs.batch_intake import filter_sources_by_chart_names

    sources = [
        ("/drop/chart_a", "chart_a", "local"),
        ("/drop/chart_b", "chart_b", "local"),
        ("/drop/chart_c", "chart_c", "local"),
    ]
    matched, missing = filter_sources_by_chart_names(
        sources, ["chart_c", "nope", "chart_a", "chart_a"]
    )
    assert [n for _s, n, _m in matched] == ["chart_c", "chart_a"]
    assert missing == ["nope"]

    all_kept, no_missing = filter_sources_by_chart_names(sources, None)
    assert all_kept == sources
    assert no_missing == []


def test_batch_runs_charts_in_alphabetical_order():
    """Order is by chart name only — size does not reorder the batch."""
    from jobs.batch_intake import LARGE_CHART_MIN_PAGES, submission_order

    big = LARGE_CHART_MIN_PAGES + 1
    sources = [(f"src/{n}", n, "blob") for n in ("delta", "Alpha", "charlie", "bravo")]
    pages = [10, big, 300, 5]

    rows = submission_order(sources, pages)

    assert [r[2] for r in rows] == ["Alpha", "bravo", "charlie", "delta"]
    assert [r[0] for r in rows] == [1, 2, 3, 4], "N/X label follows the run order"
    assert [r[4] for r in rows] == [True, False, False, False]


def test_large_means_more_than_the_threshold():
    """500 pages is not large; 501 is (default LARGE_CHART_MIN_PAGES=500)."""
    from jobs.batch_intake import LARGE_CHART_MIN_PAGES, is_large_chart

    assert not is_large_chart(LARGE_CHART_MIN_PAGES)
    assert is_large_chart(LARGE_CHART_MIN_PAGES + 1)


def _run_one(monkeypatch, *, force, skip_completed):
    """Drive _run_one_chart for a chart the DB says is finished."""
    import threading
    from contextlib import contextmanager

    import db
    import db.chart_status as chart_status
    from jobs import batch_intake as bi

    ran: list[str] = []

    @contextmanager
    def fake_connect():
        yield object()

    monkeypatch.setattr(db, "connect", fake_connect)
    monkeypatch.setattr(db, "get_chart_by_name", lambda conn, name: {"id": 1, "chart_name": name})
    monkeypatch.setattr(chart_status, "chart_is_pipeline_complete", lambda conn, cid: True)
    import orchestrator.runner as runner

    monkeypatch.setattr(
        runner, "ingest_and_run",
        lambda **kw: ran.append(kw["blob_path"]) or {"chart_id": 1, "chart_name": "c1"},
    )
    monkeypatch.setattr(bi, "_note_progress", lambda *a, **k: None)
    entry = bi._run_one_chart(
        1, 1, "Raw/c1", "c1", "blob",
        blob_container="cont", local_write_path=None, blob_write_path=None,
        write_mode="skip_orig_pages", overwrite=True, force=force,
        run_pipeline=True, only=None, through=None, skip_ocr=not force,
        redownload_pages=False, skip_completed=skip_completed,
        run_id=None, batch_id=None,
        counters={"started": 0, "finished": 0}, counter_lock=threading.Lock(),
    )
    return entry, ran


def test_skip_ocr_still_runs_finished_charts(monkeypatch):
    """skip_ocr turns force off internally; that must NOT skip finished charts.
    Regression: a 500-chart skip_ocr batch ran 0 charts ('already complete')."""
    entry, ran = _run_one(monkeypatch, force=False, skip_completed=False)
    assert ran == ["Raw/c1"]
    assert entry["status"] == "completed"


def test_skip_completed_skips_a_finished_chart(monkeypatch):
    entry, ran = _run_one(monkeypatch, force=True, skip_completed=True)
    assert ran == []
    assert entry["status"] == "skipped"
    assert entry["skip_reason"] == "already_complete"


def test_refresh_runs_finished_charts_first():
    from jobs.batch_intake import submission_order

    sources = [(f"/in/{n}", n, "local") for n in ("a", "b", "c", "d")]
    order = submission_order(sources, [1, 1, 1, 1], first={"d", "b"})
    assert [name for _pos, _src, name, *_ in order] == ["b", "d", "a", "c"]
    assert [pos for pos, *_ in order] == [1, 2, 3, 4]
    # Without a refresh set the order stays alphabetical.
    plain = submission_order(sources, [1, 1, 1, 1])
    assert [name for _pos, _src, name, *_ in plain] == ["a", "b", "c", "d"]
