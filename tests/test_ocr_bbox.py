"""Bounding-box / polygon helpers for Final1 (Docling) and Final2 (Azure)."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "core-pipeline"
for path in (str(CORE), str(ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)


def test_docling_page_sizes_from_dict_pages():
    from stages.lib.imaging.docling_ocr import _page_sizes_map

    page = SimpleNamespace(size=SimpleNamespace(width=1000.0, height=2000.0))
    doc = SimpleNamespace(pages={1: page})
    sizes = _page_sizes_map(doc)
    assert sizes[1] == (1000.0, 2000.0)
    assert sizes[0] == (1000.0, 2000.0)  # 0-based alias for prov.page_no


def test_docling_header_norm_with_dict_pages():
    from stages.lib.imaging.docling_ocr import extract_section_headers

    class _BBox:
        # TOPLEFT image coords: t < b
        l, t, r, b = 100.0, 200.0, 400.0, 240.0

    class _Prov:
        bbox = _BBox()
        coord_origin = "TOPLEFT"
        page_no = 0

    class _Item:
        label = "section_header"
        text = "HISTORY OF PRESENT ILLNESS"
        prov = [_Prov()]

    page = SimpleNamespace(size=SimpleNamespace(width=1000.0, height=2000.0))
    doc = SimpleNamespace(
        pages={1: page},
        iterate_items=lambda: [(_Item(), 1)],
    )
    headers = extract_section_headers(doc)
    assert len(headers) == 1
    assert headers[0]["norm"] is not None
    assert headers[0]["norm"]["left"] == 0.1
    assert headers[0]["norm"]["top"] == 0.1
    assert headers[0]["norm"]["width"] == 0.3
    assert abs(headers[0]["norm"]["height"] - 0.02) < 1e-6


def test_bbox_to_css_norm_bottomleft():
    from stages.lib.imaging.docling_ocr import _bbox_to_css_norm

    # BOTTOMLEFT: t > b (y up). Box near top of a 2000-tall page.
    norm = _bbox_to_css_norm(100, 1800, 400, 1700, 1000, 2000, "BOTTOMLEFT")
    assert norm is not None
    assert norm["left"] == 0.1
    assert abs(norm["top"] - 0.1) < 1e-6  # (2000-1800)/2000
    assert norm["width"] == 0.3
    assert abs(norm["height"] - 0.05) < 1e-6


def test_resolve_norm_page_size_halves_when_page_is_2x_image():
    from stages.lib.imaging.docling_ocr import _resolve_norm_page_size

    # images_scale=2: page.size doubled, bboxes still in native pixels.
    w, h = _resolve_norm_page_size(
        2000.0,
        4000.0,
        (1000.0, 2000.0),
        [(100.0, 200.0, 400.0, 240.0)],
    )
    assert w == 1000.0
    assert h == 2000.0


def test_markdown_is_sparse_header_only_form():
    from stages.lib.imaging.docling_ocr import markdown_is_sparse

    sparse = """
Date (Printed): 5/22/2025

## PATIENT DATA

8220 STATE ROUTE 45 ORWELL OH

## EMERGENCY CONTACT

## GUARANTOR

## COVERAGE
"""
    assert markdown_is_sparse(sparse) is True
    rich = sparse + ("\nPatient Name Anderson Justin DOB 08/29/1954 " * 20)
    assert markdown_is_sparse(rich) is False


def test_review_ui_halves_page_dims_against_image():
    """OCR page_* 2× the file → scale = image/page maps boxes onto the image."""
    from app.adapters.local.repository import _section_headers_from_page

    page = {
        "fileName": "1.jpg",
        "section_headers": [
            {
                "text": "PATIENT DATA",
                "level": 2,
                # Coords in OCR page space (0..2000); image is 1000×2000.
                "bbox": [200.0, 400.0, 800.0, 480.0],
                "page_width": 2000.0,
                "page_height": 4000.0,
                "coord_origin": "TOPLEFT",
            }
        ],
    }
    headers = _section_headers_from_page(page, image_size=(1000.0, 2000.0))
    assert len(headers) == 1
    # scale_x=0.5 → image box [100,200,400,240] → fractions of 1000×2000
    assert abs(headers[0].left - 0.1) < 1e-6
    assert abs(headers[0].top - 0.1) < 1e-6
    assert abs(headers[0].width - 0.3) < 1e-6


def test_review_ui_azure_inches_not_mixed_with_image_pixels():
    """Final2 inch polygons scale onto image pixels (document-processing pattern)."""
    from app.adapters.local.repository import _section_headers_from_page

    page = {
        "fileName": "1.jpg",
        "unit": "inch",
        "section_headers": [
            {
                "text": "PATIENT DATA",
                "level": 2,
                "bbox": [1.0, 2.0, 3.0, 2.4],
                "page_width": 8.5,
                "page_height": 11.0,
                "unit": "inch",
                "coord_origin": "TOPLEFT",
            }
        ],
    }
    headers = _section_headers_from_page(page, image_size=(2550.0, 3300.0))
    assert len(headers) == 1
    assert abs(headers[0].left - (1.0 / 8.5)) < 1e-6
    assert abs(headers[0].width - (2.0 / 8.5)) < 1e-6


def test_azure_section_headers_pixel_2x_uses_image():
    from stages.ocr_final2_azure import _section_headers_from_lines

    lines = [
        {
            "content": "MEDICATIONS",
            # OCR page space at 2× native image pixels.
            "polygon": [200.0, 400.0, 800.0, 400.0, 800.0, 480.0, 200.0, 480.0],
        }
    ]
    headers = _section_headers_from_lines(
        lines,
        page_w=2000.0,
        page_h=4000.0,
        unit="pixel",
        image_size=(1000.0, 2000.0),
    )
    assert len(headers) == 1
    assert abs(headers[0]["norm"]["left"] - 0.1) < 1e-6
    assert abs(headers[0]["norm"]["width"] - 0.3) < 1e-6
    assert headers[0]["page_width"] == 1000.0


def test_azure_line_polygon_to_section_headers():
    from stages.ocr_final2_azure import (
        _flatten_polygon,
        _page_meta,
        _section_headers_from_lines,
    )

    line = SimpleNamespace(
        content="CHIEF COMPLAINT",
        polygon=[10.0, 20.0, 210.0, 20.0, 210.0, 50.0, 10.0, 50.0],
    )
    word = SimpleNamespace(
        content="CHIEF",
        confidence=0.99,
        polygon=[10.0, 20.0, 80.0, 20.0, 80.0, 50.0, 10.0, 50.0],
    )
    page = SimpleNamespace(
        page_number=1,
        angle=0.0,
        width=1000.0,
        height=2000.0,
        unit="pixel",
        lines=[line],
        words=[word],
        barcodes=[],
    )
    meta = _page_meta(page)
    assert meta["lines"][0]["polygon"] == [
        10.0, 20.0, 210.0, 20.0, 210.0, 50.0, 10.0, 50.0,
    ]
    assert meta["words"][0]["polygon"][:2] == [10.0, 20.0]

    headers = _section_headers_from_lines(
        meta["lines"], page_w=1000.0, page_h=2000.0
    )
    assert len(headers) == 1
    assert headers[0]["text"] == "Chief Complaint"
    assert headers[0]["norm"]["left"] == 0.01
    assert headers[0]["norm"]["top"] == 0.01
    assert headers[0]["norm"]["width"] == 0.2
    assert abs(headers[0]["norm"]["height"] - 0.015) < 1e-6

    assert _flatten_polygon(None) == []
    assert len(_flatten_polygon([1, 2, 3, 4, 5, 6, 7, 8])) == 8


def test_review_ui_headers_from_azure_polygon():
    from app.adapters.local.repository import _section_headers_from_page

    page = {
        "fileName": "14.jpg",
        "content": "…",
        "section_headers": [
            {
                "text": "ASSESSMENT",
                "level": 2,
                "polygon": [100, 200, 300, 200, 300, 240, 100, 240],
                "page_width": 1000,
                "page_height": 2000,
            }
        ],
    }
    headers = _section_headers_from_page(page)
    assert len(headers) == 1
    assert headers[0].text == "ASSESSMENT"
    assert headers[0].width > 0
    assert headers[0].height > 0


def test_review_ui_recomputes_norm_from_bbox_ignoring_bad_stored():
    """Older Final1 JSON defaulted coord_origin to BOTTOMLEFT on image OCR."""
    from app.adapters.local.repository import _section_headers_from_page

    page = {
        "fileName": "1.jpg",
        "section_headers": [
            {
                "text": "PATIENT DATA",
                "level": 2,
                # TOPLEFT geometry (t < b) near the top of the page.
                "bbox": [50.0, 100.0, 250.0, 140.0],
                "page_width": 1000.0,
                "page_height": 2000.0,
                "coord_origin": "BOTTOMLEFT",
                # Deliberately wrong stored norm (near the bottom).
                "norm": {"left": 0.05, "top": 0.93, "width": 0.2, "height": 0.02},
            }
        ],
    }
    headers = _section_headers_from_page(page)
    assert len(headers) == 1
    assert abs(headers[0].left - 0.05) < 1e-6
    assert abs(headers[0].top - 0.05) < 1e-6  # 100/2000, not the stored 0.93
    assert abs(headers[0].width - 0.2) < 1e-6
    assert abs(headers[0].height - 0.02) < 1e-6


def test_review_ui_headers_from_pages_meta_lines():
    from app.adapters.local.repository import (
        _filter_headers_against_canon,
        _section_headers_from_page,
    )

    page = {
        "fileName": "14.jpg",
        "content": "…",
        "pagesMeta": [
            {
                "width": 1000,
                "height": 2000,
                "lines": [
                    {
                        "content": "MEDICATIONS",
                        "polygon": [50, 100, 250, 100, 250, 130, 50, 130],
                    },
                    {
                        "content": "Patient was seen in clinic for follow up of chronic issues.",
                        "polygon": [50, 200, 900, 200, 900, 230, 50, 230],
                    },
                ],
            }
        ],
    }
    # Raw shortlist = all lines; canon match keeps Medications only.
    raw = _section_headers_from_page(page)
    assert len(raw) == 2
    headers = _filter_headers_against_canon(raw)
    assert len(headers) == 1
    assert headers[0].text == "Medications"
    assert headers[0].left == 0.05
