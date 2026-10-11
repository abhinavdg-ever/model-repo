"""Made-up images and CSVs only. No real pages ever go near these tests."""
from __future__ import annotations

import csv
import hashlib
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

TOOL = Path(__file__).resolve().parents[1]
if str(TOOL) not in sys.path:
    sys.path.insert(0, str(TOOL))


def make_page(path: Path, lines: list[str], fmt: str | None = None) -> Path:
    """A white page with large black made-up text."""
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (1200, 900), "white")
    draw = ImageDraw.Draw(image)
    try:
        from PIL import ImageFont

        font = ImageFont.load_default(size=48)
    except TypeError:  # older Pillow
        font = None
    for i, line in enumerate(lines):
        draw.text((60, 60 + i * 90), line, fill="black", font=font)
    image.save(path, fmt) if fmt else image.save(path)
    return path


def write_labels(root: Path, rows: list[dict[str, str]], columns: list[str] | None = None) -> Path:
    columns = columns or ["image", "page_type", "page_subtype", "model_type"]
    path = root / "image_labels.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    return path


def read_labels(root: Path) -> list[dict[str, str]]:
    with (root / "image_labels.csv").open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture()
def folder(tmp_path: Path) -> Path:
    """Two tagged pages, one untagged page in a sub-folder, one row with no file."""
    make_page(tmp_path / "70000001_Pg1.jpg", ["PROGRESS NOTE", "Chief complaint made up"])
    make_page(tmp_path / "70000001_Pg2.jpg", ["LABORATORY RESULTS", "Sodium made up"])
    make_page(tmp_path / "sub" / "IMG_0001.png", ["DISCHARGE SUMMARY", "Made up text"])
    write_labels(
        tmp_path,
        [
            {"image": "70000001_Pg1.jpg", "page_type": "Progress Note",
             "page_subtype": "Progress Note", "model_type": "Progress Note"},
            {"image": "70000001_Pg2.jpg", "page_type": "Laboratory Data",
             "page_subtype": "Laboratory Data", "model_type": "Laboratory Data"},
            {"image": "IMG_0001.png", "page_type": "", "page_subtype": "", "model_type": ""},
            {"image": "gone.jpg", "page_type": "Forms", "page_subtype": "Forms", "model_type": "Forms"},
        ],
    )
    return tmp_path
