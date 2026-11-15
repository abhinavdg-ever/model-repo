"""The anchor pages a chart is described by (first cut, 2026-11-15).

- ``first_pn``: the first page of the chart's first Progress Note. With the pipeline's
  final.csv: the first kept page whose page type is Progress Note. Without it: the first kept
  page that carries 2+ progress-note section markers (Chief Complaint, HPI, Assessment, Plan ...).
- ``last``: the chart's last kept page with text.
- Fallback for both: the first kept page with text.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from chart_clustering.ingest import Chart, Page

PN_MARKERS = re.compile(
    r"(?<![a-z])(?:chief\s+complaint|history\s+of\s+present\s+illness|hpi|review\s+of\s+systems|ros"
    r"|physical\s+exam(?:ination)?|assessment(?:\s+and\s+plan)?|plan|subjective|objective|vitals?"
    r"|progress\s+note|reason\s+for\s+visit|impression)(?![a-z])",
    re.IGNORECASE)
MIN_WORDS = 15


@dataclass
class Anchor:
    page: Optional[Page]
    how: str  # "progress_note_type" / "progress_note_markers" / "last_page" / "fallback" / "none"


def _usable(page: Page) -> bool:
    return page.kept and len(page.text.split()) >= MIN_WORDS


def is_progress_note(page: Page, typed: bool) -> bool:
    if typed:
        return page.page_type.strip().lower().startswith("progress note")
    return len({m.group(0).lower() for m in PN_MARKERS.finditer(page.text)}) >= 2


def first_progress_note(chart: Chart) -> Anchor:
    typed = chart.has_pipeline_csv
    for page in chart.pages:
        if _usable(page) and is_progress_note(page, typed):
            return Anchor(page, "progress_note_type" if typed else "progress_note_markers")
    return _fallback(chart)


def last_page(chart: Chart) -> Anchor:
    for page in reversed(chart.pages):
        if _usable(page):
            return Anchor(page, "last_page")
    return _fallback(chart)


def _fallback(chart: Chart) -> Anchor:
    for page in chart.pages:
        if page.text.strip():
            return Anchor(page, "fallback")
    return Anchor(None, "none")


def progress_note_share(chart: Chart) -> float:
    kept = [p for p in chart.pages if p.kept and p.text.strip()]
    if not kept:
        return 0.0
    return sum(is_progress_note(p, chart.has_pipeline_csv) for p in kept) / len(kept)
