"""Page lines with positions, and section headers, from the OCR JSON files.

Final2 (Azure) gives lines with polygons and the page size, so it is preferred.
Final1 (Docling) gives words with polygons; they are grouped into lines here,
because its markdown is not in top-to-bottom order and its first lines are not
reliably the page header. A page with neither falls back to its text lines.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

from config import ocr_dir

from .engine import Line, normalize

logger = logging.getLogger(__name__)


def load_pages(chart_name: str, engine: str) -> dict[str, dict[str, Any]]:
    """``{file name: page}`` from ``<chart>_<engine>.json``; empty when absent."""
    path: Path = ocr_dir(chart_name) / f"{chart_name}_{engine}.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Unreadable OCR JSON %s: %s", path, exc)
        return {}
    return {
        str(page.get("fileName") or ""): page
        for page in data.get("pages") or []
        if isinstance(page, dict)
    }


def _extent(polygon: Any, height: float) -> Optional[tuple[float, float]]:
    try:
        ys = [float(y) for y in polygon[1::2]]
    except (TypeError, ValueError):
        return None
    if not ys or height <= 0:
        return None
    return min(ys) / height, max(ys) / height


def lines_from_final2(page: dict[str, Any]) -> list[Line]:
    meta = (page.get("pagesMeta") or [None])[0]
    if not meta:
        return []
    height = float(meta.get("height") or 0)
    out = []
    for line in meta.get("lines") or []:
        extent = _extent(line.get("polygon"), height)
        text = normalize(str(line.get("content") or ""))
        if extent and text:
            out.append(Line(text, *extent))
    return sorted(out, key=lambda l: (l.top, l.bottom))


def lines_from_final1(page: dict[str, Any]) -> list[Line]:
    """Words grouped into lines: a word joins the line whose vertical middle
    it overlaps, in left-to-right order."""
    height = float(page.get("height") or 0)
    words = []
    for word in page.get("words") or []:
        polygon = word.get("polygon")
        extent = _extent(polygon, height)
        text = str(word.get("content") or "").strip()
        if extent and text:
            words.append((extent[0], extent[1], float(polygon[0]), text))
    words.sort(key=lambda w: ((w[0] + w[1]) / 2, w[2]))

    grouped: list[list[tuple[float, float, float, str]]] = []
    for word in words:
        middle = (word[0] + word[1]) / 2
        if grouped:
            last = grouped[-1]
            top = min(w[0] for w in last)
            bottom = max(w[1] for w in last)
            if top <= middle <= bottom:
                last.append(word)
                continue
        grouped.append([word])

    out = []
    for group in grouped:
        group.sort(key=lambda w: w[2])
        text = normalize(" ".join(w[3] for w in group))
        if text:
            out.append(Line(text, min(w[0] for w in group), max(w[1] for w in group)))
    return out


def section_headers(page: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    """The page's matched section headers with their top position."""
    out = []
    for header in (page or {}).get("section_headers") or []:
        top = (header.get("norm") or {}).get("top")
        out.append(
            {
                "text": str(header.get("text") or ""),
                "matched_canonical": str(header.get("matched_canonical") or ""),
                "top": round(float(top), 4) if top is not None else None,
            }
        )
    return out


def page_layout(
    final2: Optional[dict[str, Any]],
    final1: Optional[dict[str, Any]],
) -> tuple[list[Line], list[dict[str, Any]], str]:
    """(positioned lines, section headers, which engine they came from)."""
    for page, read, name in ((final2, lines_from_final2, "final2"), (final1, lines_from_final1, "final1")):
        if page:
            lines = read(page)
            if lines:
                return lines, section_headers(page), name
    return [], section_headers(final2 or final1), ""
