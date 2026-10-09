"""Family training keeps a label only when it is clearly ahead."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PREPARE = REPO / "training" / "page-classification" / "data-prep" / "scripts" / "prepare_training_text.py"
TRAIN = REPO / "training" / "page-classification" / "training-script" / "train_family.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


prepare = _load("prepare_training_text", PREPARE)
train_family = _load("train_family", TRAIN)


def test_blank_and_junk_rows_are_skipped():
    assert prepare.is_blank_or_junk({"page_type": "Blank"})
    assert prepare.is_blank_or_junk({"page_type": "junk"})
    assert not prepare.is_blank_or_junk({"page_type": "Progress Note"})


def test_jpeg_is_preferred_over_heic(tmp_path: Path):
    (tmp_path / "Pg2.HEIC").write_bytes(b"heic")
    (tmp_path / "Pg2.jpg").write_bytes(b"jpg")
    assert prepare.index_scans(tmp_path)["Pg2"].suffix == ".jpg"


def test_threshold_accepts_the_top_family():
    decision = train_family.decide({"Progress Note": 0.62, "Radiology Report": 0.30})
    assert decision["page_family"] == "Progress Note"
    assert decision["reason"] == "above threshold"


def test_a_lead_without_enough_probability_abstains():
    decision = train_family.decide({"Laboratory Report": 0.40, "Progress Note": 0.12})
    assert decision["page_family"] is None
    assert decision["reason"] == "abstain"


def test_a_close_call_abstains():
    decision = train_family.decide({"Progress Note": 0.58, "Radiology Report": 0.42})
    assert decision["page_family"] is None
    assert decision["reason"] == "abstain"


def test_unknown_and_singleton_families_are_left_out(tmp_path: Path):
    import json
    path = tmp_path / "rows.jsonl"
    rows = [
        {"page_family": "Unknown", "raw_text": "blank page", "chart_id": "a", "page_id": "a"},
        {"page_family": "Progress Note", "raw_text": "office visit", "chart_id": "b", "page_id": "b1"},
        {"page_family": "Progress Note", "raw_text": "soap note", "chart_id": "c", "page_id": "b2"},
        {"page_family": "Wound Care Note", "raw_text": "wound", "chart_id": "d", "page_id": "d"},
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    used, dropped = train_family.eligible(train_family.load_rows(path), 2)
    assert [row["page_family"] for row in used] == ["Progress Note", "Progress Note"]
    assert dropped == ["Wound Care Note"]
