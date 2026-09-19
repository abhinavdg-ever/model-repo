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
