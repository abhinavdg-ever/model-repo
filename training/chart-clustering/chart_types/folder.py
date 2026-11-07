"""Read a training folder shaped like core-pipeline/outputs/charts: one subfolder per chart.

Per chart the only required file is the Tesseract text, ``ocr/<chart>_prelim.txt`` with
``===== N.jpg =====`` page separators (or ``text/<page>.txt``, one file per page). When the
chart was run through the pipeline, ``imaging/<chart>_final.csv`` (page type, specialty,
blank/junk, duplicates, handwriting) and ``imaging/<chart>_quality.csv`` (DPI) are read too.

Nothing here keeps page text: a page leaves this module as numbers and labels only.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

SEPARATOR = re.compile(r"^=====\s*(.+?)\s*=====\s*$", re.MULTILINE)
_WORD = re.compile(r"[A-Za-z]{2,}")
_VOWEL = re.compile(r"[aeiouy]", re.IGNORECASE)
_LABEL_LINE = re.compile(r"^\s*[A-Za-z][A-Za-z /&()#.-]{1,40}:\s*\S")
_FAX = re.compile(r"\b(fax|page\s+\d+\s+of\s+\d+|from:|to:)\b", re.IGNORECASE)

FINAL_COLUMNS = ("page_type", "page_subtype", "specialty", "blank_junk_flag", "is_duplicate",
                 "printed_or_handwritten", "handwritten_area_pct", "document_seq")


@dataclass
class Chart:
    name: str
    folder: Path
    pages: dict[str, str] = field(default_factory=dict)      # page_name -> text (in memory only)
    final: dict[str, dict] = field(default_factory=dict)     # page_name -> final.csv row
    dpi: dict[str, str] = field(default_factory=dict)        # page_name -> input_dpi
    images: int = 0


def split_pages(text: str) -> dict[str, str]:
    """``===== 1.jpg =====`` blocks -> {"1.jpg": text}. Text before the first marker is dropped."""
    marks = list(SEPARATOR.finditer(text))
    out: dict[str, str] = {}
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        out[m.group(1)] = text[m.end():end].strip("\n")
    return out


def _first(folder: Path, pattern: str) -> Path | None:
    hits = sorted(p for p in folder.glob(pattern) if not p.name.startswith("._"))
    return hits[0] if hits else None


def _rows(path: Path | None) -> dict[str, dict]:
    if path is None:
        return {}
    with path.open(encoding="utf-8", newline="") as fh:
        return {r.get("page_name", ""): r for r in csv.DictReader(fh)}


def read_chart(folder: Path) -> Chart | None:
    chart = Chart(folder.name, folder)
    prelim = _first(folder / "ocr", "*_prelim.txt")
    if prelim is not None:
        chart.pages = split_pages(prelim.read_text(encoding="utf-8", errors="replace"))
    elif (folder / "text").is_dir():
        chart.pages = {p.stem: p.read_text(encoding="utf-8", errors="replace")
                       for p in sorted((folder / "text").glob("*.txt")) if not p.name.startswith("._")}
    if not chart.pages:
        return None
    chart.final = _rows(_first(folder / "imaging", "*_final.csv"))
    chart.dpi = {k: r.get("input_dpi", "") for k, r in _rows(_first(folder / "imaging", "*_quality.csv")).items()}
    for sub in ("pages", "corrected-pages"):
        if (folder / sub).is_dir():
            chart.images = sum(1 for p in (folder / sub).iterdir()
                               if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".tif", ".tiff"}
                               and not p.name.startswith("._"))
            break
    return chart


def iter_charts(root: Path):
    """Every chart folder under ``root`` that has page text, in name order."""
    for folder in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith((".", "_"))):
        chart = read_chart(folder)
        if chart is not None:
            yield chart


def text_traits(text: str) -> dict[str, float]:
    """Capture traits from the text alone (no images): size, garbling, label lines, fax lines."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    words = text.split()
    alpha = _WORD.findall(text)
    garbled = sum(1 for w in alpha if len(w) >= 4 and not _VOWEL.search(w))
    top = lines[: max(1, len(lines) // 7)] if lines else []
    return {
        "n_chars": len(text),
        "n_lines": len(lines),
        "n_words": len(words),
        "garbled_share": round(garbled / len(alpha), 4) if alpha else 0.0,
        "digit_share": round(sum(c.isdigit() for c in text) / len(text), 4) if text else 0.0,
        "label_line_share": round(sum(1 for ln in lines if _LABEL_LINE.match(ln)) / len(lines), 4) if lines else 0.0,
        "fax_top": int(any(_FAX.search(ln) for ln in top)),
    }
