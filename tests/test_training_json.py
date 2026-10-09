"""Collated page-type records stay within the BERT token cap."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO / "training" / "page-classification" / "data-prep" / "scripts" / "build_training_json.py"
spec = importlib.util.spec_from_file_location("build_training_json", MODULE_PATH)
assert spec is not None and spec.loader is not None
build_training_json = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = build_training_json
spec.loader.exec_module(build_training_json)

collate = build_training_json.collate
truncate_text = build_training_json.truncate_text
fill_codeability = build_training_json.fill_codeability
codeability_index = build_training_json.codeability_index


def test_text_is_cut_at_500_tokens():
    words = [f"w{i}" for i in range(600)]
    text = " ".join(words)
    cut = truncate_text(text, 500)
    assert len(cut.split()) == 500
    assert cut.split()[-1] == "w499"


def test_newlines_inside_the_cap_are_kept():
    text = "FACE SHEET\n\nPatient Name: HOLLAND, MARGUERITE A\nMRN: 4471902"
    assert truncate_text(text, 500) == text


class _WordTokenizer:
    """One token per whitespace word, with offsets into the original string."""

    def __call__(self, text, add_special_tokens=False, return_offsets_mapping=True, truncation=True, max_length=500):
        spans = []
        for token in text.split():
            start = text.find(token, spans[-1][1] if spans else 0)
            spans.append((start, start + len(token)))
        if truncation:
            spans = spans[:max_length]
        return {"offset_mapping": spans}


def test_bert_offsets_keep_the_original_prefix():
    text = "Patient Name: HOLLAND\nMRN: 1\n" + " ".join(f"n{i}" for i in range(20))
    cut = truncate_text(text, 3, tokenizer=_WordTokenizer())
    assert cut == "Patient Name: HOLLAND"


def test_csv_row_joins_a_page_text_and_builds_the_id(tmp_path: Path):
    ocr = tmp_path / "chart8841_p001.txt"
    body = "PATIENT REGISTRATION / FACE SHEET\n\nPatient Name: HOLLAND, MARGUERITE A"
    ocr.write_text(body, encoding="utf-8")
    records, problems = collate(
        [
            {
                "page_id": "chart8841_p001.png",
                "family_label": "administrative",
                "page_type_label": "Face sheet / Registration",
            }
        ],
        [(ocr, body)],
        images_root=tmp_path,
        max_tokens=500,
    )
    assert problems == []
    assert records == [
        {
            "id": "chart8841_p001",
            "text": body,
            "page_family": "administrative",
            "page_type": "Face sheet / Registration",
        }
    ]


def test_catalog_tag_fills_codeability():
    mapping = REPO / "training" / "page-classification" / "data-prep" / "mappings" / "page_labels.json"
    records = [
        {
            "id": "chart8841_p001",
            "text": "face sheet",
            "page_family": "demographics",
            "page_type": "Face sheet / Registration",
        }
    ]
    assert fill_codeability(records, codeability_index(mapping)) == []
    assert records[0]["codeability"] == "codeable"


def test_a_page_id_with_no_ocr_file_is_reported(tmp_path: Path):
    ocr = tmp_path / "chart8841_p001.txt"
    ocr.write_text("one", encoding="utf-8")
    records, problems = collate(
        [{"page_id": "missing.png", "family_label": "administrative", "page_type_label": "Face sheet / Registration"}],
        [(ocr, "one")],
        images_root=tmp_path,
    )
    assert records == []
    assert problems == ["no OCR for page_id=missing.png"]
