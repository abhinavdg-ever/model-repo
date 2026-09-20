"""Final1 fallback policy: Docling's result stands unless the page hard-fails.

Short output is the page, not a half-finished convert — only a timeout or an
exception sends a page to RapidOCR-onnx.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "core-pipeline"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

PAGE = {"id": 1, "page_name": "1.jpg", "page_number": 1}


def _docling_result(content: str) -> dict:
    return {
        "content": content,
        "markdown": content,
        "section_headers": [{"text": "Patient Data"}],
        "document": None,
        "elapsed_seconds": 2.0,
    }


def _run(monkeypatch, tmp_path, *, convert, onnx_text="rapid text"):
    import stages.lib.imaging.docling_ocr as docling_ocr
    import stages.ocr_final1_docling as final1

    img = tmp_path / "1.jpg"
    img.write_bytes(b"x")
    monkeypatch.setattr(docling_ocr, "get_converter", lambda: object())
    monkeypatch.setattr(docling_ocr, "convert_image_with_timeout", convert)
    monkeypatch.setattr(final1, "_ocr_onnx", lambda p: onnx_text)
    return final1._ocr_one((PAGE, img, True, "chart-1"))


def test_thin_docling_output_is_kept(monkeypatch, tmp_path):
    """A header-only form page used to be thrown away by the sparse check."""
    thin = "## PATIENT DATA\n\n8220 STATE ROUTE 45 ORWELL OH\n\n## COVERAGE\n"
    out = _run(
        monkeypatch,
        tmp_path,
        convert=lambda p, converter=None, timeout_seconds=None: _docling_result(thin),
    )
    assert out["engine"] == "docling+rapidocr"
    assert out["content"] == thin
    assert out["section_headers"]


def test_empty_docling_output_is_kept(monkeypatch, tmp_path):
    out = _run(
        monkeypatch,
        tmp_path,
        convert=lambda p, converter=None, timeout_seconds=None: _docling_result(""),
    )
    assert out["engine"] == "docling+rapidocr"
    assert out["content"] == ""


def test_timeout_falls_back_to_rapidocr(monkeypatch, tmp_path):
    def boom(p, converter=None, timeout_seconds=None):
        raise TimeoutError("Docling exceeded 90s on 1.jpg")

    out = _run(monkeypatch, tmp_path, convert=boom)
    assert out["engine"] == "rapidocr-onnx"
    assert out["content"] == "rapid text"


def test_crash_falls_back_to_rapidocr(monkeypatch, tmp_path):
    def boom(p, converter=None, timeout_seconds=None):
        raise RuntimeError("torch blew up")

    out = _run(monkeypatch, tmp_path, convert=boom)
    assert out["engine"] == "rapidocr-onnx"
    assert out["content"] == "rapid text"
