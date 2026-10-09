"""Page-tag checkpoint outputs → stored label, document type and review status."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "core-pipeline"
for path in (str(CORE), str(ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

np = pytest.importorskip("numpy")

from stages.lib.image_preprocess.hw_printed import (  # noqa: E402
    PAGE_TAGS,
    TAG_METHOD,
    ClassifierBundle,
    decide_tags,
    label_for_document_type,
)


def _bundle(min_conf: float = 0.5) -> ClassifierBundle:
    return ClassifierBundle(
        torch_model=None,
        device="cpu",
        uncertain_min_confidence=min_conf,
        classes=dict(enumerate(PAGE_TAGS)),
        task="page_tags",
    )


def _type_proba(top: str, p: float) -> "np.ndarray":
    rest = (1.0 - p) / (len(PAGE_TAGS) - 1)
    return np.array([p if tag == top else rest for tag in PAGE_TAGS])


@pytest.mark.parametrize(
    "document_type, area, label",
    [
        ("PRINTED", 0.0, "Printed"),
        ("HANDWRITTEN", 80.0, "Handwritten"),
        ("FORM", 12.0, "Mixed"),
        ("FORM", 1.0, "Printed"),
        ("VISUAL", 0.0, "Printed"),
        ("BLANK", 0.0, "Uncertain"),
        ("UNCERTAIN", 0.0, "Uncertain"),
    ],
)
def test_every_page_type_maps_to_a_stored_label(document_type, area, label):
    assert label_for_document_type(document_type, area) == label


def test_confident_form_keeps_its_type_and_reports_the_tags():
    page = decide_tags(_type_proba("Form", 0.8), np.array([0.9, 0.1]), 0.123, _bundle())
    assert page.document_type == "FORM"
    assert page.label == "Mixed"
    assert page.method == TAG_METHOD
    assert page.visibility == "Visible"
    assert page.handwritten_area_pct == 12.3
    assert page.confidence == 0.8
    assert page.tags() == {
        "document_type": "form",
        "handwritten_probability": page.p_handwritten,
        "is_visible": True,
        "handwritten_area_pct": 12.3,
    }


def test_weak_call_is_uncertain_but_weak_blank_is_kept():
    weak = decide_tags(_type_proba("Printed", 0.4), np.array([0.9, 0.1]), 0.0, _bundle())
    assert weak.document_type == "UNCERTAIN"
    blank = decide_tags(_type_proba("Blank", 0.4), np.array([0.9, 0.1]), 0.0, _bundle())
    assert blank.document_type == "BLANK"


def test_review_follows_quality_type_and_visibility():
    from stages.lib.image_preprocess.stage import _review_required

    assert not _review_required("high", {"document_type": "printed", "is_visible": True})
    assert not _review_required("high", {"document_type": "printed", "is_visible": None})
    assert _review_required("low", {"document_type": "printed"})
    assert not _review_required("low", {"document_type": "blank"})
    assert _review_required("high", {"document_type": "uncertain"})
    assert _review_required("high", {"document_type": "form", "is_visible": False})


def test_csvs_and_upsert_use_the_schema_column_names():
    import inspect

    from db import upsert_quality
    from stages.lib.image_preprocess.stage import HW_COLS, QUALITY_COLS

    columns = ["document_type", "handwritten_probability", "is_visible", "handwritten_area_pct"]
    assert HW_COLS[-4:] == columns
    assert QUALITY_COLS[-1] == "review_required"
    params = inspect.signature(upsert_quality).parameters
    assert all(name in params for name in [*columns, "review_required"])
    schema = (ROOT / "schema" / "v1.sql").read_text(encoding="utf-8")
    for name in [*columns, "review_required"]:
        assert f"    {name} " in schema, name


def test_decisions_read_the_page_type():
    from db import page_blocks_prelim, page_type
    from stages.lib.image_preprocess.quality_label_postprocess import (
        apply_quality_label_postprocess,
    )
    from stages.utilities.gate_delta import hw_class_from_row

    # Type wins over the old label.
    row = {"document_type": "form", "printed_or_handwritten": "printed", "quality_tag": "high"}
    assert page_type(row) == "form"
    assert page_blocks_prelim(row)
    assert hw_class_from_row(row) == "non_printed"
    visual = {"document_type": "visual", "printed_or_handwritten": "printed", "quality_tag": "high"}
    assert not page_blocks_prelim(visual)
    assert hw_class_from_row(visual) == "printed"
    # A row from before document_type existed still decides on its old label.
    assert page_type({"printed_or_handwritten": "mixed"}) == "form"
    assert page_type({"printed_or_handwritten": "printed"}) == "printed"
    assert page_type(None) == ""

    def cap(document_type):
        return apply_quality_label_postprocess(
            quality_tag="high", quality_score=0.9, document_type=document_type
        )

    assert cap("handwritten") == "medium"
    assert cap("form") == "high"


def test_page_sets_follow_the_type():
    from db.memory_store import MemoryStore

    store = MemoryStore()
    rows = {
        1: ("printed", "printed", "high"),
        2: ("visual", "printed", "high"),
        3: ("form", "mixed", "high"),
        4: ("handwritten", "handwritten", "medium"),
        5: ("blank", "uncertain", "low"),
    }
    for page_id, (doc, legacy, tag) in rows.items():
        store.upsert_quality(
            chart_id=1, page_id=page_id, printed_or_handwritten=legacy,
            quality_tag=tag, document_type=doc,
        )
    assert store.non_printed_page_ids(1) == {3, 4, 5}
    assert store.handwritten_page_ids(1) == {4}
    assert store.high_quality_printed_page_ids(1) == {1, 2}


def test_review_ui_reads_the_columns_and_the_csv_cells():
    sys.path.insert(0, str(ROOT / "review-ui" / "backend"))
    from app.services.imaging_overlays import index_hw_rows, page_type_fields

    from_db = page_type_fields("form", True, 14.2)
    assert from_db == {"documentType": "Form", "isVisible": True, "handwrittenAreaPct": 14.2}
    assert page_type_fields("printed", None, None) == {"documentType": "Printed"}

    row = {
        "chart_name": "c", "page_name": "1.jpg", "page_number": "1",
        "handwritten_or_printed": "mixed", "confidence": "0.81",
        "document_type": "form", "handwritten_probability": "0.12",
        "is_visible": "False", "handwritten_area_pct": "14.2",
    }
    hit = index_hw_rows([row], "c")["#1"]
    assert hit["documentType"] == "Form"
    assert hit["isVisible"] is False
    assert hit["handwrittenAreaPct"] == 14.2
    assert "handwrittenProbability" not in hit
