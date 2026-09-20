"""Tests for SKIP_OCR disk detection (no database)."""
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
