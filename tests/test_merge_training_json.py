"""utilities/merge_training_json.py, with made-up records only."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("merge_training_json", REPO / "utilities" / "merge_training_json.py")
merge_json = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = merge_json  # dataclasses look the module up by name
_spec.loader.exec_module(merge_json)


def rec(image, text, model_type="Progress Note", size=100, **extra):
    return {"image": image, "text": text, "model_type": model_type, "page_type": model_type,
            "page_subtype": model_type, "file_size": size, **extra}


def write(path: Path, records) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


def read(path: Path):
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]


def test_same_page_is_kept_once_and_the_later_file_wins(tmp_path):
    a = write(tmp_path / "a.jsonl", [rec("1.jpg", "made up one"), rec("2.jpg", "made up two")])
    b = write(tmp_path / "b.jsonl", [rec("2.jpg", "made up two", model_type="Forms"), rec("3.jpg", "made up three")])
    out = tmp_path / "out.jsonl"
    assert merge_json.main([str(a), str(b), "--out", str(out), "--taxonomy", str(tmp_path / "none.json")]) == 0
    rows = read(out)
    assert [r["image"] for r in rows] == ["1.jpg", "2.jpg", "3.jpg"]
    assert rows[1]["model_type"] == "Forms"


def test_same_name_but_a_different_file_keeps_both(tmp_path):
    a = write(tmp_path / "a.jsonl", [rec("IMG_1.HEIC", "made up page A", size=100)])
    b = write(tmp_path / "b.jsonl", [rec("IMG_1.HEIC", "made up page B", size=222)])
    records, report = merge_json.merge([a, b])
    assert len(records) == 2
    assert report.same_name_other_file == ["IMG_1.HEIC"]


def test_identical_text_under_two_names_is_reported_and_optionally_dropped(tmp_path):
    a = write(tmp_path / "a.jsonl", [rec("x.jpg", "same made up text"), rec("y.jpg", "same   made up text")])
    records, report = merge_json.merge([a])
    assert len(records) == 2 and report.duplicate_text == [("x.jpg", "y.jpg")]
    records, report = merge_json.merge([a], drop_duplicate_text=True)
    assert [r["image"] for r in records] == ["x.jpg"]


def test_bad_lines_and_missing_fields_are_dropped_and_counted(tmp_path):
    path = tmp_path / "a.jsonl"
    path.write_text('{"image": "1.jpg", "text": "made up", "model_type": "Forms"}\nnot json\n'
                    '{"image": "2.jpg", "text": ""}\n', encoding="utf-8")
    records, report = merge_json.merge([path])
    assert len(records) == 1
    assert report.missing_fields == 1 and sum(report.bad_lines.values()) == 1


def test_unknown_model_types_are_counted(tmp_path):
    a = write(tmp_path / "a.jsonl", [rec("1.jpg", "made up", model_type="Not A Type")])
    _, report = merge_json.merge([a], model_types={"Progress Note"})
    assert report.unknown_model_types == {"Not A Type": 1}


def test_a_folder_input_takes_every_jsonl_and_output_can_be_an_input(tmp_path):
    write(tmp_path / "in" / "one" / "training_data.jsonl", [rec("1.jpg", "made up one")])
    write(tmp_path / "in" / "two" / "training_data.jsonl", [rec("2.jpg", "made up two")])
    out = tmp_path / "in" / "one" / "training_data.jsonl"
    merge_json.main([str(tmp_path / "in"), "--out", str(out), "--taxonomy", str(tmp_path / "none.json")])
    assert [r["image"] for r in read(out)] == ["1.jpg", "2.jpg"]


def test_no_page_text_is_printed(tmp_path, capsys):
    a = write(tmp_path / "a.jsonl", [rec("1.jpg", "SECRET MADE UP TEXT"), rec("2.jpg", "SECRET MADE UP TEXT")])
    merge_json.main([str(a), "--out", str(tmp_path / "o.jsonl"), "--dry-run"])
    out = capsys.readouterr().out
    assert "SECRET" not in out
    assert not (tmp_path / "o.jsonl").exists()
