"""Blank / faint pages skip ConvNeXt. Spread ink can upgrade Printed."""
from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "core-pipeline"
for path in (str(CORE), str(ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")
from PIL import Image  # noqa: E402

from stages.lib.image_preprocess.hw_printed import (  # noqa: E402
    content_before_model,
    upgrade_printed_with_ink,
)


def _png(image: Image.Image) -> bytes:
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def _white(width: int = 800, height: int = 1100) -> Image.Image:
    return Image.new("RGB", (width, height), (255, 255, 255))


def test_blank_page_is_uncertain_and_skips_the_model():
    from stages.lib.image_preprocess.hw_printed import classify_image_type

    label, confidence, method = classify_image_type(_png(_white()))
    assert (label, confidence, method) == ("Uncertain", 0.80, "blank_page")


def test_faint_strokes_are_uncertain():
    image = _white()
    draw = image.load()
    # Thin pencil-gray lines, inset so they are not a scanner border.
    for y in range(200, 700, 40):
        for x in range(80, 700):
            draw[x, y] = (160, 160, 160)
            draw[x, y + 1] = (160, 160, 160)
    label, confidence, method = content_before_model(image)
    assert (label, confidence, method) == ("Uncertain", 0.80, "faint_marks_only")


def test_dark_ink_reaches_the_model():
    image = _white()
    draw = image.load()
    for y in range(200, 800, 12):
        for x in range(80, 720):
            draw[x, y] = (0, 0, 0)
            draw[x, y + 1] = (0, 0, 0)
    assert content_before_model(image) is None


def test_spread_ink_upgrades_printed_to_handwritten():
    # Diagonals: straight rules are stripped as form lines, so the ink check
    # only sees strokes that are tall and spread down the page.
    image = _white(900, 800)
    draw = image.load()
    for i, y in enumerate(range(60, 700, 55)):
        x0 = 80 + (i % 4) * 50
        for t in range(48):
            x = x0 + t
            yy = y + t // 2
            for dx in range(3):
                for dy in range(3):
                    if x + dx < 900 and yy + dy < 800:
                        draw[x + dx, yy + dy] = (0, 0, 0)
    label, _confidence, method = upgrade_printed_with_ink(image, "Printed", 0.9, 0.1)
    assert label == "Handwritten"
    assert method == "page_convnext_plus_ink"


def test_header_mark_does_not_upgrade():
    image = _white(1200, 1600)
    draw = image.load()
    for yy in range(30, 80):
        for x in range(80, 200):
            draw[x, yy] = (0, 0, 0)
    label, confidence, method = upgrade_printed_with_ink(image, "Printed", 0.91, 0.09)
    assert (label, confidence, method) == ("Printed", 0.91, "convnext_tiny")


def test_handwritten_model_label_is_kept():
    image = _white()
    label, confidence, method = upgrade_printed_with_ink(
        image, "Handwritten", 0.8, 0.8
    )
    assert (label, confidence, method) == ("Handwritten", 0.8, "convnext_tiny")
