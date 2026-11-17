"""The chart model and the local-folder reader (the database and Blob readers are in
sources.py; all three give the same Chart).

A chart folder is shaped like core-pipeline/outputs/charts (and like a chart's Blob output prefix):

<root>/<chart_name>/ocr/<chart_name>_prelim.txt      required: Tesseract text, pages split by
                                                      "===== <page name> =====" lines
<root>/<chart_name>/imaging/<chart_name>_final.csv  optional: page type, blank / junk,
                                                      duplicate, handwriting per page
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional

PAGE_MARKER = re.compile(r"^=====\s*(.+?)\s*=====\s*$", re.MULTILINE)


@dataclass
class Page:
    name: str
    number: int
    text: str
    # From imaging/<chart>_final.csv when present.
    page_type: str = ""
    blank_junk: str = ""
    duplicate: bool = False
    handwritten: Optional[bool] = None
    # The continuity document the page belongs to (final.csv / imaging_final document_seq).
    document: str = ""

    @property
    def kept(self) -> bool:
        """A page the pipeline kept: not blank, junk or a duplicate (unknown = kept)."""
        flag = self.blank_junk.strip().lower()
        return flag in ("", "not_blank_junk", "main") and not self.duplicate


@dataclass
class Chart:
    name: str
    pages: list[Page] = field(default_factory=list)
    has_pipeline_csv: bool = False
    # Where the page images are (chart_list, --source db): the Raw_Input folder (blob_path) and
    # the Processed folder (output_path). The visual track reads them.
    blob_container: str = ""
    blob_path: str = ""
    output_path: str = ""


def _page_number(name: str, fallback: int) -> int:
    digits = re.findall(r"\d+", name)
    return int(digits[-1]) if digits else fallback


def split_prelim(text: str) -> list[tuple[str, str]]:
    """(page name, page text) in file order."""
    marks = list(PAGE_MARKER.finditer(text))
    out = []
    for i, mark in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        out.append((mark.group(1).strip(), text[mark.end():end].strip()))
    return out


def _truthy(value: str) -> bool:
    return str(value).strip().lower() in ("true", "yes", "y", "1")


def final_rows_from_csv(text: str) -> dict[str, dict[str, str]]:
    """imaging/<chart>_final.csv text -> page name -> row."""
    import io

    return {str(row.get("page_name") or "").strip(): row for row in csv.DictReader(io.StringIO(text))}


def chart_from_pages(name: str, pages: list[tuple[str, str]], rows: dict[str, dict[str, str]]) -> Chart:
    """A Chart from (page name, Tesseract text) pairs and optional final.csv-shaped rows
    (page_number, page_type, blank_junk_flag, is_duplicate, printed_or_handwritten)."""
    chart = Chart(name=name, has_pipeline_csv=bool(rows))
    for i, (page_name, text) in enumerate(pages, 1):
        row = rows.get(page_name, {})
        hw = str(row.get("printed_or_handwritten") or "").strip().lower()
        number = str(row.get("page_number") or "").strip()
        chart.pages.append(Page(
            name=page_name,
            number=int(number) if number.isdigit() else _page_number(page_name, i),
            text=text or "",
            page_type=str(row.get("page_type") or ""),
            blank_junk=str(row.get("blank_junk_flag") or ""),
            duplicate=_truthy(row.get("is_duplicate") or ""),
            handwritten=(hw in ("handwritten", "mixed")) if hw else None,
            document=str(row.get("document_seq") or "").strip(),
        ))
    chart.pages.sort(key=lambda p: p.number)
    return chart


def read_chart(folder: Path) -> Optional[Chart]:
    """A local chart folder (ocr/<chart>_prelim.txt, optional imaging/<chart>_final.csv)."""
    name = folder.name
    prelim = folder / "ocr" / f"{name}_prelim.txt"
    if not prelim.is_file():
        return None
    final_csv = folder / "imaging" / f"{name}_final.csv"
    rows = final_rows_from_csv(final_csv.read_text(encoding="utf-8")) if final_csv.is_file() else {}
    return chart_from_pages(name, split_prelim(prelim.read_text(encoding="utf-8", errors="replace")), rows)


def read_root(root: Path, chart_list: Optional[list[str]] = None) -> Iterator[Chart]:
    """Every chart under ``root`` (or only ``chart_list``), sorted by name. Folders without a
    prelim text are skipped."""
    wanted = set(chart_list) if chart_list else None
    for folder in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith((".", "._"))):
        if wanted is not None and folder.name not in wanted:
            continue
        chart = read_chart(folder)
        if chart is not None and chart.pages:
            yield chart
