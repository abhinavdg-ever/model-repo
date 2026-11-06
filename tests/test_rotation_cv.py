"""2026-11-03 (user): rotation_cv is a second opinion on the coarse turn. OSD and it agreeing
(0 included) decide the turn; otherwise the reading rule decides, as before."""
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")
from stages.lib.image_preprocess import rotation, stage  # noqa: E402
from stages.lib.image_preprocess import osd as osd_module  # noqa: E402
from stages.lib.image_preprocess.rotation_cv import detect_rotation  # noqa: E402


def _text_page() -> np.ndarray:
    page = np.full((1400, 1000, 3), 255, np.uint8)
    for row in range(40):
        cv2.putText(page, "The patient was seen today and has a history of", (40, 60 + row * 32),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 0), 2)
    return page


def test_rotation_cv_reads_a_turned_page():
    page = _text_page()
    assert detect_rotation(page).rotation == 0
    sideways = cv2.rotate(page, cv2.ROTATE_90_COUNTERCLOCKWISE)  # needs 90 CW to be upright
    assert detect_rotation(sideways).rotation == 90


def _run(monkeypatch, tmp_path, osd_turn, cv_turn, reads=None):
    path = tmp_path / "p.png"
    cv2.imwrite(str(path), _text_page())
    called = {"reading_rule": False}

    def reading_rule(small, proposed):
        called["reading_rule"] = True
        return 0, False, SimpleNamespace(share=0.9, tokens=10, reads=True, english=10)

    monkeypatch.setattr(osd_module, "detect_rotation", lambda image: {"rotation": osd_turn, "confidence": 5.0})
    monkeypatch.setattr(stage, "_cv_result", lambda image: (cv_turn, {"rotation": cv_turn}))
    monkeypatch.setattr(stage, "_classify_hw", lambda p: ("printed", 0.9, "model", {"document_type": "printed"}))
    monkeypatch.setattr(rotation, "orientation_that_reads", reading_rule)
    def read_page(small, deg, mirrored=False, **kw):
        share, tokens = (reads or {}).get(mirrored, (0.9, 40))
        return SimpleNamespace(share=share, tokens=tokens, reads=share >= 0.5 and tokens >= 5,
                               english=round(share * tokens))

    monkeypatch.setattr(rotation, "read_page", read_page)
    monkeypatch.setattr(rotation, "tilt_on_upright", lambda image, deg, mirrored=False: 0.0)
    monkeypatch.setattr(rotation, "tilt_that_reads", lambda *a, **k: 0.0)
    return stage._detect_rotation(path), called["reading_rule"]


def test_agreeing_on_a_turn_turns_the_page(monkeypatch, tmp_path):
    out, reading = _run(monkeypatch, tmp_path, 90, 90)
    assert (out["orientation_angle"], out["method"], reading) == (90.0, "osd_cv_agree", False)


def test_disagreeing_goes_through_the_reading_rule(monkeypatch, tmp_path):
    out, reading = _run(monkeypatch, tmp_path, 90, 0)
    assert reading and out["method"] == "readability" and out["orientation_angle"] == 0.0
    out, reading = _run(monkeypatch, tmp_path, 0, 0)  # both 0: stays 0, no reading search
    assert (out["orientation_angle"], out["method"], reading) == (0.0, "osd_cv_agree", False)


def _run_cv_side(monkeypatch, tmp_path, cv_turn, search_turn, reads_by_turn, document_type="visual"):
    """OSD 0, CV ``cv_turn``; the reading search returns ``search_turn``; reads per turn."""
    path = tmp_path / "p.png"
    cv2.imwrite(str(path), _text_page())

    def page_read(english):
        return SimpleNamespace(share=0.9 if english else 0.1, tokens=max(english, 10),
                               reads=english >= 8, english=english)

    monkeypatch.setattr(osd_module, "detect_rotation", lambda image: {"rotation": 0, "confidence": 5.0})
    monkeypatch.setattr(stage, "_cv_result", lambda image: (cv_turn, {"rotation": cv_turn}))
    monkeypatch.setattr(stage, "_classify_hw", lambda p: ("printed", 0.9, "model", {"document_type": document_type}))
    monkeypatch.setattr(rotation, "orientation_that_reads",
                        lambda small, proposed: (search_turn, False, page_read(reads_by_turn[search_turn])))
    monkeypatch.setattr(rotation, "read_page", lambda small, deg, mirrored=False, **kw: page_read(reads_by_turn[deg]))
    monkeypatch.setattr(rotation, "tilt_on_upright", lambda image, deg, mirrored=False: 0.0)
    monkeypatch.setattr(rotation, "tilt_that_reads", lambda *a, **k: 0.0)
    return stage._detect_rotation(path)


def test_cv_quarter_turn_is_final_on_a_visual_page(monkeypatch, tmp_path):
    """2026-11-06 (user): on a visual page CV's 90 / 270 is final, whatever OSD says or the page
    reads. A printed page goes through the reading rule as before."""
    out = _run_cv_side(monkeypatch, tmp_path, 90, 0, {0: 60, 90: 3})
    assert (out["orientation_angle"], out["method"]) == (90.0, "cv_final")
    out = _run_cv_side(monkeypatch, tmp_path, 270, 0, {0: 60, 270: 3})
    assert (out["orientation_angle"], out["method"]) == (270.0, "cv_final")
    out = _run_cv_side(monkeypatch, tmp_path, 90, 0, {0: 9, 90: 3}, document_type="printed")
    assert (out["orientation_angle"], out["method"]) == (0.0, "osd")


def test_both_zero_tries_the_mirror_only_when_the_page_reads_badly(monkeypatch, tmp_path):
    # Reads well at 0: no flip tried, stays 0 unmirrored.
    out, _ = _run(monkeypatch, tmp_path, 0, 0, reads={False: (0.9, 40), True: (0.95, 60)})
    assert out["mirrored"] is False
    # Reads badly at 0, the flipped page reads: mirrored.
    out, reading = _run(monkeypatch, tmp_path, 0, 0, reads={False: (0.1, 40), True: (0.9, 40)})
    assert (out["orientation_angle"], out["mirrored"], reading) == (0.0, True, False)
    # Neither reads: stays as it is.
    out, _ = _run(monkeypatch, tmp_path, 0, 0, reads={False: (0.1, 40), True: (0.2, 40)})
    assert out["mirrored"] is False


def test_rotation_log_lists_only_turned_pages(tmp_path, monkeypatch):
    """2026-11-04 (user): logs/rotation.log, one line per page OSD or OpenCV turns."""
    from stages.lib.image_preprocess import stage

    monkeypatch.setenv("LOG_DIR", str(tmp_path))

    def item(name, osd, cv):
        return {"page_name": name, "rot": {"debug": {
            "osd": {"rotation": osd, "confidence": 3.2}, "opencv": {"rotation": cv},
            "agree": osd == cv, "decision": {"turn": osd or 0, "mirrored": False, "method": "osd"}}}}

    stage._append_rotation_log("c1", [item("1.jpg", 0, 0), item("2.jpg", 90, 90), item("3.jpg", 0, 180)])
    lines = (tmp_path / "rotation.log").read_text(encoding="utf-8").splitlines()
    assert [line.split("\t")[2] for line in lines] == ["page=2.jpg", "page=3.jpg"]
    assert "osd=90" in lines[0] and "opencv=90" in lines[0] and "agree=yes" in lines[0]
    assert "opencv=180" in lines[1] and "agree=no" in lines[1]
