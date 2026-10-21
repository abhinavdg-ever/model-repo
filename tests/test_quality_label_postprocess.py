"""Quality label post-process: Handwritten + High → Medium; Docling/Tesseract overlap."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core-pipeline"))

from stages.lib.image_preprocess.quality_label_postprocess import (  # noqa: E402
    apply_quality_label_postprocess,
    base_quality_label,
    downgrade_by_ocr_overlap,
    ocr_overlap,
)


def test_base_quality_label():
    assert base_quality_label(0.80) == "high"
    assert base_quality_label(0.50) == "medium"
    assert base_quality_label(0.20) == "low"
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


def test_regrade_runs_before_the_azure_skip_and_never_twice():
    import json

    from db.memory_store import MemoryStore
    from stages.lib.ocr.stage_final2 import regrade_by_ocr_overlap

    store = MemoryStore()
    docling = {1: "one two three four", 2: "one two three four", 3: "one two"}
    tesseract = {1: "one two three four", 2: "one two five six seven eight", 3: "zzz"}
    for page_id in (1, 2, 3, 4):
        store.upsert_quality(
            chart_id=1, page_id=page_id, printed_or_handwritten="printed",
            quality_tag="high", document_type="printed", review_required=False,
        )
        if page_id in docling:
            store.upsert_ocr_result(
                chart_id=1, page_id=page_id, ocr_type="docling",
                raw_text=json.dumps({"content": docling[page_id]}),
            )
            store.upsert_ocr_result(
                chart_id=1, page_id=page_id, ocr_type="tesseract",
                raw_text=tesseract[page_id],
            )

    assert regrade_by_ocr_overlap(store, 1) == 2
    assert regrade_by_ocr_overlap(store, 1) == 0
    quality = store.get_quality_map(1)
    assert {pid: row["quality_tag"] for pid, row in quality.items()} == {
        1: "high", 2: "medium", 3: "low", 4: "high",
    }
    assert quality[3]["review_required"] is True
    assert quality[2]["quality_detail"]["ocr_overlap"]["tag_before"] == "high"
    assert store.high_quality_printed_page_ids(1) == {1, 4}
