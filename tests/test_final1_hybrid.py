"""Unit tests for Final1 hybrid fallback (headers + Rapid text)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "core-pipeline"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))


def test_get_converter_caches_distinct_cell_matching_modes(monkeypatch):
    import stages.lib.imaging.docling_ocr as docling_ocr

    docling_ocr.reset_converters_for_tests()
    monkeypatch.setenv("DOCLING_TABLE_CELL_MATCHING", "true")

    built: list[bool] = []

    def fake_build(*, cell_matching=None, models_dir=None):
        key = docling_ocr._resolve_cell_matching(cell_matching)
        built.append(key)
        return f"converter-{key}"

    monkeypatch.setattr(docling_ocr, "docling_status", lambda: {"ready": True, "models_dir": "/m"})
    monkeypatch.setattr(docling_ocr, "build_converter", fake_build)

    assert docling_ocr.get_converter(True) == "converter-True"
    assert docling_ocr.get_converter(False) == "converter-False"
    assert docling_ocr.get_converter(True) == "converter-True"  # cached
    assert built == [True, False]

    docling_ocr.reset_converters_for_tests()


def test_get_converter_none_follows_env(monkeypatch):
    import stages.lib.imaging.docling_ocr as docling_ocr

    docling_ocr.reset_converters_for_tests()
    monkeypatch.setenv("DOCLING_TABLE_CELL_MATCHING", "false")

    built: list[bool] = []

    def fake_build(*, cell_matching=None, models_dir=None):
        key = docling_ocr._resolve_cell_matching(cell_matching)
        built.append(key)
        return f"converter-{key}"

    monkeypatch.setattr(docling_ocr, "docling_status", lambda: {"ready": True, "models_dir": "/m"})
    monkeypatch.setattr(docling_ocr, "build_converter", fake_build)

    assert docling_ocr.get_converter() == "converter-False"
    assert built == [False]
    docling_ocr.reset_converters_for_tests()


def test_ocr_one_timeout_skips_hybrid_uses_rapid(tmp_path, monkeypatch):
    """After a Docling timeout the orphan still runs — never start hybrid."""
    from stages import ocr_final1_docling as final1

    image = tmp_path / "1.jpg"
    image.write_bytes(b"fake")
    page = {"id": 7, "page_name": "1.jpg", "page_number": 1}
    hybrid_calls: list[str] = []

    primary = object()

    def fake_get_converter(cell_matching=None):
        return primary

    def fake_convert(image_path, *, converter=None, timeout_seconds=30.0):
        raise TimeoutError("Docling exceeded 30s on 1.jpg")

    monkeypatch.setattr(
        "stages.lib.imaging.docling_ocr.get_converter", fake_get_converter
    )
    monkeypatch.setattr(
        "stages.lib.imaging.docling_ocr.convert_image_with_timeout", fake_convert
    )
    monkeypatch.setattr(final1, "_ocr_onnx", lambda path: "rapid body text")
    monkeypatch.setattr(
        final1,
        "_hybrid_headers_and_rapid",
        lambda *a, **k: hybrid_calls.append("no") or {},
    )

    out = final1._ocr_one((page, image, True, "chartA"))

    assert out["error"] == ""
    assert out["content"] == "rapid body text"
    assert out["section_headers"] == []
    assert out["engine"] == "rapidocr-onnx"
    assert hybrid_calls == []


def test_ocr_one_hybrid_on_sparse(tmp_path, monkeypatch):
    from stages import ocr_final1_docling as final1

    image = tmp_path / "1.jpg"
    image.write_bytes(b"fake")
    page = {"id": 7, "page_name": "1.jpg", "page_number": 1}
    headers = [
        {
            "text": "Patient Data",
            "level": 2,
            "bbox": [10, 20, 100, 40],
            "norm": {"left": 0.1, "top": 0.1, "width": 0.2, "height": 0.05},
        }
    ]

    primary = object()
    fast = object()

    def fake_get_converter(cell_matching=None):
        if cell_matching is False:
            return fast
        return primary

    def fake_convert(image_path, *, converter=None, timeout_seconds=30.0):
        if converter is primary:
            return {
                "content": "HDR",
                "markdown": "HDR",
                "section_headers": [],
                "document": None,
                "elapsed_seconds": 2.0,
            }
        if converter is fast:
            return {
                "content": "# Patient Data\n",
                "markdown": "# Patient Data\n",
                "section_headers": headers,
                "document": None,
                "elapsed_seconds": 4.0,
            }
        raise AssertionError(f"unexpected converter {converter!r}")

    monkeypatch.setattr(
        "stages.lib.imaging.docling_ocr.get_converter", fake_get_converter
    )
    monkeypatch.setattr(
        "stages.lib.imaging.docling_ocr.convert_image_with_timeout", fake_convert
    )
    monkeypatch.setattr(final1, "_ocr_onnx", lambda path: "rapid body text")

    out = final1._ocr_one((page, image, True, "chartA"))

    assert out["error"] == ""
    assert out["content"] == "rapid body text"
    assert out["section_headers"] == headers
    assert out["engine"] == final1.HYBRID_ENGINE


def test_ocr_one_rapid_only_when_hybrid_headers_fail(tmp_path, monkeypatch):
    from stages import ocr_final1_docling as final1

    image = tmp_path / "2.jpg"
    image.write_bytes(b"fake")
    page = {"id": 8, "page_name": "2.jpg", "page_number": 2}

    primary = object()
    fast = object()

    def fake_get_converter(cell_matching=None):
        return fast if cell_matching is False else primary

    def fake_convert(image_path, *, converter=None, timeout_seconds=30.0):
        if converter is primary:
            return {
                "content": "x",
                "markdown": "x",
                "section_headers": [],
                "elapsed_seconds": 1.0,
            }
        raise RuntimeError("fast Docling also failed")

    monkeypatch.setattr(
        "stages.lib.imaging.docling_ocr.get_converter", fake_get_converter
    )
    monkeypatch.setattr(
        "stages.lib.imaging.docling_ocr.convert_image_with_timeout", fake_convert
    )
    monkeypatch.setattr(final1, "_ocr_onnx", lambda path: "rapid only")

    out = final1._ocr_one((page, image, True, "chartB"))
    assert out["content"] == "rapid only"
    assert out["section_headers"] == []
    assert out["engine"] == "rapidocr-onnx"


def test_ocr_one_primary_success_skips_hybrid(tmp_path, monkeypatch):
    from stages import ocr_final1_docling as final1

    image = tmp_path / "3.jpg"
    image.write_bytes(b"fake")
    page = {"id": 9, "page_name": "3.jpg", "page_number": 3}
    called_onnx = []

    primary = object()

    def fake_get_converter(cell_matching=None):
        assert cell_matching is None or cell_matching is True
        return primary

    def fake_convert(image_path, *, converter=None, timeout_seconds=30.0):
        return {
            "content": "A" * 200,
            "markdown": "A" * 200,
            "section_headers": [{"text": "H"}],
            "document": None,
            "elapsed_seconds": 2.5,
        }

    monkeypatch.setattr(
        "stages.lib.imaging.docling_ocr.get_converter", fake_get_converter
    )
    monkeypatch.setattr(
        "stages.lib.imaging.docling_ocr.convert_image_with_timeout", fake_convert
    )
    monkeypatch.setattr(
        final1, "_ocr_onnx", lambda path: called_onnx.append(path) or "should-not"
    )

    out = final1._ocr_one((page, image, True, "chartC"))
    assert out["engine"] == "docling+rapidocr"
    assert out["content"] == "A" * 200
    assert out["section_headers"] == [{"text": "H"}]
    assert called_onnx == []
