"""EB (2026-11-28, user): in pass 1, a page with only one line at the top and / or one at the bottom
and nothing between is Blank, settled in pass 1 (pass 2 does not undo it). Two lines at an edge
("DME Orders / None recorded.") is not."""
from __future__ import annotations

from pathlib import Path

import modules.blank_junk.stage as stage  # noqa: F401  — puts the blank_junk lib on sys.path

import method  # noqa: E402
from method import PageInputs, decide  # noqa: E402

HEADER = "Patient Name: Madeup Eli Date of Birth: 01/01/1990 Sex: F Encounter ID: 123 Encounter Date: 12/16/2025"


def run(edge, text=HEADER + "\n3", pass_no=1, **kw):
    return decide(PageInputs(text=text, pass_no=pass_no, document_type="printed", edge_lines=edge, **kw))


def test_one_line_top_and_bottom_is_blank_in_pass_1():
    v = run({"top": 1, "bottom": 1, "body_ink": False})
    assert v.code == method.CODE_BLANK and v.rule == "R1d", v.reason
    assert run({"top": 1, "bottom": 0, "body_ink": False}, text=HEADER).rule == "R1d"
    assert run({"top": 0, "bottom": 1, "body_ink": False}, text="Page 3 of 9").rule == "R1d"


def test_not_blank_when():
    assert run({"top": 2, "bottom": 0, "body_ink": False}, text="DME Orders\nNone recorded.").rule != "R1d"
    assert run({"top": 1, "bottom": 2, "body_ink": False}).rule != "R1d"
    assert run({"top": 1, "bottom": 1, "body_ink": True}).rule != "R1d"      # ink between
    assert run({"top": 1, "bottom": 1, "body_ink": None}).rule != "R1d"      # not measured
    assert run(None).rule != "R1d"
    assert run({"top": 1, "bottom": 1, "body_ink": False}, text=HEADER + "\nJane Roe MD").rule != "R1d"
    assert run({"top": 1, "bottom": 1, "body_ink": False}, pass_no=2).rule != "R1d"  # pass 1 only


def test_settled_in_pass_1():
    """A pass-1 Blank on a page pass 1 judges is in pass1_ruled_out: pass 2 skips it."""
    from apps.worker.stages._support import BJ_EXCLUDE
    assert "blank" in BJ_EXCLUDE


def _page(tmp_path: Path, lines_at: list[float], height: int = 1100, width: int = 850) -> Path:
    from PIL import Image, ImageDraw

    image = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(image)
    for top in lines_at:
        y = int(top * height)
        draw.rectangle((60, y, width - 60, y + 8), fill=0)
    path = tmp_path / "13.png"
    image.save(path)
    return path


def test_edge_text_lines_counts_lines_per_band(monkeypatch, tmp_path):
    for tops, expected in (([0.02, 0.96], (1, 1)), ([0.02, 0.06], (2, 0)), ([0.02], (1, 0)),
                           ([0.02, 0.5, 0.96], (1, 1))):
        path = _page(tmp_path, tops)
        monkeypatch.setattr(stage, "page_image_path", lambda chart, page, p=path: p)
        got = stage.edge_text_lines("c", "13.png")
        assert (got["top"], got["bottom"]) == expected, (tops, got)
        assert got["body_ink"] is (0.5 in tops), (tops, got)
