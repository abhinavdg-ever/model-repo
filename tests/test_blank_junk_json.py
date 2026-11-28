"""blank_junk.json (2026-11-28, user): one file per chart with each page's stats, every check of
each pass in order, the short page's checks, the model, skips, the duplicate and the final."""
from __future__ import annotations

import json

import modules.blank_junk.stage as stage  # noqa: F401  — puts the blank_junk lib on sys.path

import method  # noqa: E402
from method import PageInputs, decide  # noqa: E402


def _row(page_id, name, inputs):
    v = decide(inputs)
    return stage._row_for({"id": page_id, "page_name": name, "page_number": page_id}, code=v.code,
                          reason=v.reason, confidence=v.confidence, votes=v.votes, strength=v.strength)


def test_one_file_with_checks_stats_skips_and_final(monkeypatch, tmp_path):
    monkeypatch.setattr(stage, "staging_dir", lambda chart: tmp_path)
    (tmp_path / stage.DETAIL_FILE).write_text("{}", encoding="utf-8")
    p1 = [_row(1, "1.png", PageInputs(text="Patient: X\n3", pass_no=1, document_type="printed",
                                       edge_lines={"top": 1, "bottom": 1, "body_ink": False})),
          _row(2, "2.png", PageInputs(text="Progress note\n" + "plan follow up " * 30, pass_no=1,
                                       document_type="printed"))]
    stage._write_votes("c", 1, p1, skipped={"3.png": "handwritten"})
    p2 = [_row(2, "2.png", PageInputs(text="Progress note\n" + "plan follow up " * 30, pass_no=2,
                                       document_type="printed"))]
    stage._write_votes("c", 2, p2, skipped={"1.png": "blank_junk_pass1"})
    body = json.loads((tmp_path / "blank_junk.json").read_text(encoding="utf-8"))
    assert not (tmp_path / stage.DETAIL_FILE).exists()
    pages = body["pages"]
    assert list(pages) == ["1.png", "2.png", "3.png"]

    one = pages["1.png"]
    assert one["final"]["decision"] == "Blank" and one["final"]["rule"] == "R1d" and one["final"]["pass"] == 1
    assert one["pass_2"] == "skipped: settled in pass 1"
    checks = {c["check"]: c["result"] for c in one["pass_1"]["checks"]}
    assert checks["R1a no ink"] == "no" and checks["R1d one line at the top / bottom"].startswith("fired")
    assert checks["R2c tiny page"] == "not reached"
    assert one["stats"]["text"]["first_line"] == "Patient: X" and one["stats"]["text"]["last_line"] == "3"

    two = pages["2.png"]
    assert two["final"]["pass"] == 2 and two["pass_1"]["rule"] == "R0"
    names = [c["check"] for c in two["pass_2"]["checks"]]
    assert names == [method.check_label(c) for c in method.PASS2_CHECKS]
    assert "model" in two["pass_2"] and "short_page" in two["pass_2"]
    assert pages["3.png"]["pass_1"].startswith("skipped: handwritten")
    assert body["check_order"]["pass_2_short"] == [method.check_label(c) for c in method.SHORT_CHECKS]
    assert two["pass_2"]["route"] == "general -> others"
