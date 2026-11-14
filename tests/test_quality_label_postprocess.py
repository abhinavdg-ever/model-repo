"""Quality label post-process: Handwritten + High → Medium; Docling/Tesseract overlap."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core-pipeline"))

from modules.image_preprocess.quality_label_postprocess import (  # noqa: E402
    apply_quality_label_postprocess,
    base_quality_label,
    downgrade_by_ocr_overlap,
    ocr_overlap,
)


def test_base_quality_label():
    assert base_quality_label(0.70) == "high"
    assert base_quality_label(0.69) == "medium"
    assert base_quality_label(0.40) == "medium"
    assert base_quality_label(0.39) == "low"
    assert base_quality_label(None) is None


def test_handwritten_high_becomes_medium():
    assert (
        apply_quality_label_postprocess(
            quality_tag="high",
            quality_score=0.85,
            document_type="handwritten",
        )
        == "medium"
    )


def test_printed_high_stays_high():
    assert (
        apply_quality_label_postprocess(
            quality_tag="high",
            quality_score=0.85,
            document_type="printed",
        )
        == "high"
    )


def test_handwritten_low_unchanged():
    assert (
        apply_quality_label_postprocess(
            quality_tag="low",
            quality_score=0.2,
            document_type="handwritten",
        )
        == "low"
    )


def test_ocr_overlap():
    assert ocr_overlap("Patient Name: John", "patient name john") == 1.0
    assert ocr_overlap("a b c d", "a b x y") == 0.5
    assert ocr_overlap("a b", "") == 0.0
    assert ocr_overlap("", "") is None


def test_downgrade_by_ocr_overlap():
    assert downgrade_by_ocr_overlap("high", 0.50) == "high"
    assert downgrade_by_ocr_overlap("high", 0.49) == "medium"
    assert downgrade_by_ocr_overlap("medium", 0.30) == "low"
    assert downgrade_by_ocr_overlap("low", 0.30) == "low"
    assert downgrade_by_ocr_overlap("high", 0.09) == "low"
    assert downgrade_by_ocr_overlap("high", None) == "high"
    assert downgrade_by_ocr_overlap(None, 0.0) is None


def _row(doc="printed", hw=None, tag="high", legacy=None, hw_method=None):
    return {"document_type": doc, "handwritten_area_pct": hw, "quality_tag": tag,
            "printed_or_handwritten": legacy or doc, "hw_method": hw_method}


SAME = "alpha beta gamma delta epsilon zeta eta theta iota kappa"


def test_routing_table_rows():
    """2026-11-14 (user): the Doc Intel routing table, row by row (modules/ocr/routing.py)."""
    from modules.ocr.routing import route

    def r(row, q, docling=SAME, tess=SAME, settled=False):
        d = route(row, quality=q, settled=settled, docling=docling, tesseract=tess)
        return d["row"], d["final_quality"], d["docintel"]

    half = "alpha beta gamma delta epsilon zeta eta theta iota other"  # 90% overlap
    assert r(_row(hw=2), "high") == (1, "high", False)
    assert r(_row(hw=2), "high", tess=half) == (1, "high", False)
    assert r(_row(hw=2), "high", tess="alpha beta gamma") == (2, "medium", True)
    assert r(_row(hw=2), "medium") == (3, "high", False)
    assert r(_row(hw=2), "medium", tess=half) == (4, "medium", True)
    assert r(_row(hw=2), "low") == (5, "low", True)
    assert r(_row(hw=25), "high") == (6, "high", True)
    assert r(_row(doc="handwritten", hw=60), "medium") == (7, "medium", True)
    assert r(_row(doc="form", hw=1), "high") == (8, "high", True)
    assert r(_row(doc="visual", hw=1), "medium") == (9, "high", False)
    assert r(_row(doc="visual", hw=1), "high", docling="few words", tess="other text") == (9, "high", False)
    assert r(_row(doc="visual", hw=1), "low") == (10, "medium", True)
    assert r(_row(doc="visual", hw=1), "high", docling=SAME + " " + SAME * 3, tess="zzz") == (10, "medium", True)
    assert r(_row(doc="uncertain"), "high") == (11, "high", True)
    assert r(_row(hw=2), "high", docling="") == (11, "high", True)  # no Docling text: always Doc Intel
    assert r(_row(hw=2), "high", settled=True) == (12, "high", False)
    assert r(_row(hw=2), None, settled=True) == (12, "low", False)


def test_handwritten_share_decides_printed_or_handwritten():
    """2026-11-14 (user): over 10% handwriting is never Printed, under 10% never Handwritten."""
    from modules.ocr.routing import page_kind

    assert page_kind(_row(hw=12)) == "mixed"
    assert page_kind(_row(hw=45)) == "handwritten"
    assert page_kind(_row(doc="handwritten", hw=5)) == "printed"
    assert page_kind(_row(doc="handwritten")) == "handwritten"
    assert page_kind(_row(doc="form", hw=80)) == "form"
    assert page_kind({"document_type": None, "printed_or_handwritten": "mixed"}) == "mixed"
    from modules.image_preprocess.hw_printed import label_for_document_type

    assert label_for_document_type("PRINTED", 12.0) == "Mixed"
    assert label_for_document_type("HANDWRITTEN", 5.0) == "Printed"
    assert label_for_document_type("PRINTED", 50.0) == "Handwritten"
    assert label_for_document_type("PRINTED", None) == "Printed"


def test_three_engine_regrade_after_doc_intel():
    """Rows 6, 7, 8, 11: < 50% average overlap one level down, < 10% Low; other rows unchanged."""
    from modules.ocr.routing import after_docintel

    d = {"row": 7, "quality_before": "high", "final_quality": "high"}
    assert after_docintel(d, [SAME, SAME, SAME])["final_quality"] == "high"
    assert after_docintel(d, [SAME, SAME, "zzz"])["final_quality"] == "medium"
    assert after_docintel(d, ["aaa", "bbb", "ccc"])["final_quality"] == "low"
    assert after_docintel({"row": 2, "final_quality": "medium"}, ["a", "b", "c"])["final_quality"] == "medium"


def test_pass_1_skips_rows_5_to_10():
    from modules.ocr.routing import judged_in_pass2

    assert not judged_in_pass2(_row(hw=2, tag="high"))
    assert judged_in_pass2(_row(hw=2, tag="low"))
    assert judged_in_pass2(_row(hw=20))
    assert judged_in_pass2(_row(doc="visual"))
    assert judged_in_pass2(_row(doc="form"))
    assert judged_in_pass2(_row(doc="uncertain"))
    assert not judged_in_pass2(_row(doc="blank"))
    assert not judged_in_pass2(_row(doc="handwritten", hw_method="blank_page"))


def test_route_pages_stores_the_row_and_never_regrades_twice():
    import json

    from common.db.memory_store import MemoryStore
    from modules.ocr.stage_final2 import route_pages

    store = MemoryStore()
    for page_id, tess in ((1, SAME), (2, "alpha beta")):
        store.upsert_quality(chart_id=1, page_id=page_id, printed_or_handwritten="printed",
                             quality_tag="high", document_type="printed", review_required=False)
        store.upsert_ocr_result(chart_id=1, page_id=page_id, ocr_type="docling", raw_text=json.dumps({"content": SAME}))
        store.upsert_ocr_result(chart_id=1, page_id=page_id, ocr_type="tesseract", raw_text=tess)
    for _ in range(2):
        decisions = route_pages(store, 1, settled=set())
        assert {pid: d["row"] for pid, d in decisions.items()} == {1: 1, 2: 2}
    quality = store.get_quality_map(1)
    assert quality[2]["quality_tag"] == "medium"
    assert quality[2]["quality_detail"]["routing"]["quality_before"] == "high"
