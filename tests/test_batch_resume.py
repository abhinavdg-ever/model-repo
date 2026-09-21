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
    from stages import download_blob

    src = inspect.getsource(download_blob.run_download)
    # Wipe only under force=True — not `force or had_local`.
    assert "if force or had_local" not in src
    assert "if force:" in src
    assert "Blob resume" in src or "force=false" in src


def test_local_resume_keeps_existing_pages():
    from stages import download_blob

    src = inspect.getsource(download_blob.import_local_folder)
    assert "existing and not force" in src
    assert "resumed" in src
    assert "clear_chart_workspace" in src  # still used on force=True


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
