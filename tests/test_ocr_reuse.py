"""Tests for SKIP_OCR disk / output-folder detection (no database)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "core-pipeline"
for path in (str(CORE), str(ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)


def test_ocr_artifacts_present(tmp_path, monkeypatch):
    import config
    from stages.ocr_reuse import ocr_artifacts_present

    monkeypatch.setattr(config, "DATA_ROOT", tmp_path)
    chart = "chartA"
    ocr = tmp_path / chart / "ocr"
    assert ocr_artifacts_present(chart) is False

    ocr.mkdir(parents=True)
    assert ocr_artifacts_present(chart) is False

    (ocr / f"{chart}_prelim.txt").write_text(
        "===== 1.jpg =====\nhello\n", encoding="utf-8"
    )
    assert ocr_artifacts_present(chart) is True


def test_ocr_artifacts_final1_json(tmp_path, monkeypatch):
    import config
    from stages.ocr_reuse import ocr_artifacts_present

    monkeypatch.setattr(config, "DATA_ROOT", tmp_path)
    chart = "chartB"
    ocr = tmp_path / chart / "ocr"
    ocr.mkdir(parents=True)
    (ocr / f"{chart}_final1.json").write_text(
        json.dumps(
            {
                "recordId": chart,
                "pages": [{"fileName": "1.jpg", "content": "text"}],
            }
        ),
        encoding="utf-8",
    )
    assert ocr_artifacts_present(chart) is True


def test_page_doc_from_raw_preserves_json_envelope():
    from stages.ocr_reuse import _page_doc_from_raw

    raw = json.dumps(
        {
            "pageNumber": 3,
            "fileName": "old.jpg",
            "content": "hello",
            "pagesMeta": [{"width": 10}],
            "section_headers": [{"text": "H"}],
        }
    )
    doc = _page_doc_from_raw(
        raw, page_name="3.jpg", page_number=3, ocr_type="azuredocintel"
    )
    assert doc["fileName"] == "3.jpg"
    assert doc["content"] == "hello"
    assert doc["pagesMeta"] == [{"width": 10}]
    assert doc["section_headers"][0]["text"] == "H"


def test_should_skip_prefers_disk_then_needs_db(tmp_path, monkeypatch):
    import config
    from stages.ocr_reuse import should_skip_ocr_stages

    monkeypatch.setattr(config, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(config, "SKIP_OCR", False)
    chart = "chartC"
    assert (
        should_skip_ocr_stages(chart_name=chart, force=False, skip_ocr=True) is False
    )

    ocr = tmp_path / chart / "ocr"
    ocr.mkdir(parents=True)
    (ocr / f"{chart}_prelim.txt").write_text(
        "===== 1.jpg =====\nhi\n", encoding="utf-8"
    )
    assert (
        should_skip_ocr_stages(chart_name=chart, force=False, skip_ocr=True) is True
    )
    assert (
        should_skip_ocr_stages(chart_name=chart, force=True, skip_ocr=True) is False
    )


def test_copy_ocr_from_output_folder_into_workspace(tmp_path, monkeypatch):
    import config
    from stages.ocr_reuse import (
        _copy_ocr_dir_into_workspace,
        _local_output_ocr_candidates,
        ocr_artifacts_in_dir,
        ocr_artifacts_present,
    )

    monkeypatch.setattr(config, "DATA_ROOT", tmp_path / "workspace")
    chart = "FolderName"
    out = tmp_path / "Processed" / "Run1" / "Batch1" / chart
    out_ocr = out / "ocr"
    out_ocr.mkdir(parents=True)
    (out_ocr / f"{chart}_final1.json").write_text(
        json.dumps({"pages": [{"fileName": "1.jpg", "content": "x"}]}),
        encoding="utf-8",
    )
    assert ocr_artifacts_in_dir(out_ocr, chart) is True
    assert any(
        ocr_artifacts_in_dir(c, chart)
        for c in _local_output_ocr_candidates(str(out), chart)
    )
    assert ocr_artifacts_present(chart) is False
    assert _copy_ocr_dir_into_workspace(out_ocr, chart) == 1
    assert ocr_artifacts_present(chart) is True


def test_large_chart_limiter_serializes_large_while_smalls_remain():
    import threading
    import time

    from jobs.batch_intake import LargeChartLimiter

    limiter = LargeChartLimiter(small_remaining=1)
    order: list[str] = []
    lock = threading.Lock()
    first_in = threading.Event()

    def large(tag: str) -> None:
        limiter.enter(True)
        with lock:
            order.append(f"{tag}-in")
        if tag == "A":
            first_in.set()
            time.sleep(0.12)
        with lock:
            order.append(f"{tag}-out")
        limiter.leave(True)

    t_a = threading.Thread(target=large, args=("A",))
    t_b = threading.Thread(target=large, args=("B",))
    t_a.start()
    assert first_in.wait(timeout=2)
    t_b.start()
    time.sleep(0.05)
    with lock:
        assert "B-in" not in order
    t_a.join(timeout=2)
    t_b.join(timeout=2)
    assert order.index("A-out") < order.index("B-in")


def test_large_chart_limiter_allows_parallel_large_when_only_large_left():
    import threading
    import time

    from jobs.batch_intake import LargeChartLimiter

    limiter = LargeChartLimiter(small_remaining=0)
    both_in = threading.Event()
    in_count = {"n": 0}
    lock = threading.Lock()

    def large() -> None:
        limiter.enter(True)
        with lock:
            in_count["n"] += 1
            if in_count["n"] >= 2:
                both_in.set()
        time.sleep(0.08)
        limiter.leave(True)

    threads = [threading.Thread(target=large) for _ in range(2)]
    for t in threads:
        t.start()
    assert both_in.wait(timeout=2)
    for t in threads:
        t.join(timeout=2)
