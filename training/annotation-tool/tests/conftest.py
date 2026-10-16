"""Made-up images, CSVs and a made-up taxonomy only. No real pages ever go near these tests."""
from __future__ import annotations

import csv
import hashlib
import json
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

TOOL = Path(__file__).resolve().parents[1]
if str(TOOL) not in sys.path:
    sys.path.insert(0, str(TOOL))

# The shape of taxonomy_v24.json with invented names, so the tests do not move
# when the client's workbook does.
TAXONOMY = {
    "version": "test",
    "rules": {
        "section_L1": "Note", "fallback": "Others", "specialty_unknown": "Unknown",
        "specialty_other": "Others", "codeability_values": ["codeable", "non_codeable", "discharge", "needs_review"],
        "codeability_default": "needs_review",
    },
    "L1": [
        {"name": "Note", "description": "a note"},
        {"name": "Result", "description": "a result"},
        {"name": "Flat", "description": "no L2", "codeability": "needs_review"},
    ],
    "L2": {
        "Note": [{"name": "History", "codeability": "codeable"}, {"name": "Signature", "codeability": "codeable"},
                 {"name": "Others", "codeability": "codeable"}],
        "Result": [{"name": "Imaging", "codeability": "non_codeable"}, {"name": "Letter", "codeability": "non_codeable"}],
        "Flat": [],
    },
    "L3": {"Result > Imaging": [{"name": "CT", "codeability": "non_codeable"},
                                {"name": "Echo", "codeability": "codeable"},
                                {"name": "Others", "codeability": "non_codeable"}]},
    "specialty": [{"name": "Cardiology"}, {"name": "Eye"}, {"name": "Others"}, {"name": "Unknown"}],
    "paths": [],
    "counts": {"L2": 5, "L3": 3},
}

COLUMNS = ["image", "chart_id", "page_index", "L1", "L2", "L3", "path", "specialty", "codeability",
           "codeability_rule", "codeability_reason", "page_type", "dominant_section", "sections",
           "canon_suggests", "label_agrees_with_text", "annotator_page_type", "annotator_page_subtype"]


def make_page(path: Path, lines: list[str], fmt: str | None = None) -> Path:
    """A white page with large black made-up text."""
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (600, 400), "white")
    draw = ImageDraw.Draw(image)
    for i, line in enumerate(lines):
        draw.text((30, 30 + i * 40), line, fill="black")
    image.save(path, fmt) if fmt else image.save(path)
    return path


def write_labels(root: Path, rows: list[dict[str, str]], columns: list[str] | None = None) -> Path:
    path = root / "image_labels.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns or COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in (columns or COLUMNS)})
    return path


def read_labels(root: Path) -> list[dict[str, str]]:
    with (root / "image_labels.csv").open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture()
def taxonomy(tmp_path_factory):
    from taxonomy import Taxonomy

    path = tmp_path_factory.mktemp("tax") / "taxonomy_v24.json"
    path.write_text(json.dumps(TAXONOMY), encoding="utf-8")
    return Taxonomy(path)


def row(image, l1, l2="", l3="", specialty="Unknown", **extra):
    path = " > ".join(p for p in (l1, l2, l3) if p)
    return {"image": image, "L1": l1, "L2": l2, "L3": l3, "path": path, "specialty": specialty,
            "codeability": extra.pop("codeability", "codeable"),
            "codeability_rule": extra.pop("codeability_rule", "page_type_tag"),
            "codeability_reason": "made up", **extra}


@pytest.fixture()
def folder(tmp_path: Path) -> Path:
    """Four labelled pages (one flagged, one with a blank L3), one untagged
    page in a sub-folder, one row whose file is gone, and a training file
    written the way retag.py writes it (no file_size, derived fields)."""
    for name in ("70000001_Pg1.jpg", "70000001_Pg2.jpg", "70000001_Pg3.jpg", "IMG_0002.png"):
        make_page(tmp_path / name, ["MADE UP", name])
    make_page(tmp_path / "sub" / "IMG_0001.png", ["MADE UP"])
    rows = [
        row("70000001_Pg1.jpg", "Note", "History", page_type="Visit", dominant_section="Past History",
            sections="past_history"),
        row("70000001_Pg2.jpg", "Result", "Imaging", "CT", "Cardiology", codeability="non_codeable",
            page_type="Scan Report", canon_suggests="Visit", label_agrees_with_text="n",
            dominant_section="Past History", annotator_page_type="Radiology", annotator_page_subtype="CT"),
        row("70000001_Pg3.jpg", "Result", "Imaging", "", codeability="non_codeable", page_type="Scan Report"),
        row("IMG_0002.png", "Flat", codeability="needs_review", codeability_rule="context"),
        row("gone.jpg", "Note", "Signature"),
    ]
    write_labels(tmp_path, rows)
    with (tmp_path / "training_data.jsonl").open("w", encoding="utf-8") as handle:
        for r in rows[:4]:
            record = {k: r[k] for k in ("image", "L1", "L2", "L3", "path", "specialty", "codeability")}
            record.update(text=f"made up text {r['image']}", L1_train=r["L1"], L2_train=r["L2"])
            handle.write(json.dumps(record) + "\n")
    return tmp_path
