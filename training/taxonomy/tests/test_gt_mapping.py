"""The GT page-type canon (review-ui/frontend/src/gtPageTypeCanon.json) and its CSV."""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

import build_gt_mapping as gt  # noqa: E402


def _canon() -> dict:
    return json.loads(gt.CANON.read_text(encoding="utf-8"))


def test_every_target_is_a_taxonomy_path():
    assert gt.check_targets(_canon()) == []


def test_keys_and_statuses():
    labels = _canon()["labels"]
    assert all(key == gt.key(entry["label"]) for key, entry in labels.items())
    assert {entry["status"] for entry in labels.values()} <= {"mapped", "ambiguous", "exclude"}
    for entry in labels.values():
        if entry["status"] == "exclude":
            assert entry["full"] == [] and entry["partial"] == []
        else:
            assert entry["full"], entry["label"]


def test_docs_csv_is_current():
    assert gt.CSV_OUT.read_text(encoding="utf-8") == gt.to_csv(_canon())


def test_targets_split_on_the_level_separator():
    assert gt.targets("Lab Report >Diagnostic Study\nOthers\n\n") == ["Lab Report > Diagnostic Study"]
