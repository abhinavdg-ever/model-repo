"""Staging: what the extraction found, kept until the chart is done.

The extraction stage runs once, right after OCR, and finds every field the later stages
need — member name / DOB / ID, DOS, provider, e-signature, printed page number, headings.
Those stages read it from here when their turn comes, so none of them reads a page's OCR
for these fields again. Nothing in staging is a result: member verification, DOS and the
rest write their own rows, and the orchestrator drops the staging when the chart completes.

One JSON file per chart at ``<chart>/staging/extraction.json``::

    {"version": 1, "chart_name": …, "model_version": "v004", "extracted_at": …,
     "pages": {"<page name>": {"page_number": 1, "width": …, "height": …,
                               "fields": {"name": [<hit>, …], "dos": […], …}}}}

A hit is the extractor's own row (key, region, sentence, value, score, accepted, selected,
source, …) with boxes as ``[left, top, right, bottom]``. ``selected`` is the value the
extraction chose; ``accepted`` is every candidate it stands behind. When the trained model
re-decided a row, ``rule_accepted`` is what the rules said and ``model_score`` the model's
probability.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator, Optional

from config import staging_dir
from stages.lib import confidence as conf

from .util.geometry import Box

if TYPE_CHECKING:
    from .pipeline import DocumentResult

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
FILE_NAME = "extraction.json"

# Row attributes that are not data: the key a row was read from and the heading's own words.
_NOT_DATA = frozenset({"key_hit", "words"})


def staging_path(chart_name: str) -> Path:
    return staging_dir(chart_name) / FILE_NAME


def _plain(value: Any) -> Any:
    if isinstance(value, Box):
        return [round(value.left, 2), round(value.top, 2), round(value.right, 2), round(value.bottom, 2)]
    if isinstance(value, float):
        return round(value, 4)
    return value


# Set on a row when a trained version re-decided it (pipeline.apply_model_selection): what
# the rules decided and the ranker's probability.
_MODEL_ATTRS = ("rule_accepted", "model_score")


def hit_record(hit: Any) -> dict[str, Any]:
    """One extractor row as plain JSON."""
    record = {
        field.name: _plain(getattr(hit, field.name))
        for field in dataclasses.fields(hit)
        if field.name not in _NOT_DATA
    }
    for name in _MODEL_ATTRS:
        if hasattr(hit, name):
            record[name] = _plain(getattr(hit, name))
    return record


def build(chart_name: str, result: "DocumentResult", model_version: str) -> dict[str, Any]:
    pages: dict[str, Any] = {}
    for page in result.pages:
        fields = {field_id: [hit_record(hit) for hit in hits] for field_id, hits in page.rows.items()}
        text = " ".join(word.content or "" for word in page.words)
        pages[str(page.page.get("fileName") or "")] = {
            "page_number": page.page.get("pageNumber"),
            "width": page.page_w,
            "height": page.page_h,
            "fields": fields,
            "absent": absent_confidences(fields, conf.page_reasons(text) if page.words else ()),
        }
    return {
        "version": SCHEMA_VERSION,
        "chart_name": chart_name,
        "model_version": model_version,
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        "pages": pages,
    }


def absent_confidences(fields: dict[str, list[dict[str, Any]]], reasons: tuple[str, ...] = ()) -> dict[str, float]:
    """Each field the page has no accepted row for: how sure "not found" is
    (``confidence.absent``) from the candidates it turned down (their model probability,
    else their rule score)."""
    out: dict[str, float] = {}
    for field_id, rows in fields.items():
        if any(row.get("accepted") for row in rows):
            continue
        scores = [_number(row.get("model_score", row.get("score"))) for row in rows]
        value = conf.absent(scores, *reasons, module="extraction")
        if value is not None:
            out[field_id] = value
    return out


def _number(value: Any) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def write(chart_name: str, payload: dict[str, Any]) -> Path:
    """Replace the chart's staging (write to a temp name, then move, so a reader never sees half)."""
    path = staging_path(chart_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    return path


def read(chart_name: str) -> Optional["Staged"]:
    """The chart's staging, or None when the extraction has not run (or was dropped)."""
    path = staging_path(chart_name)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Unreadable extraction staging %s: %s", path, exc)
        return None
    if not isinstance(data, dict) or data.get("version") != SCHEMA_VERSION:
        logger.warning("Extraction staging %s is from another version; ignoring it", path)
        return None
    return Staged(data)


def drop(chart_name: str) -> bool:
    """Delete the chart's staging; True when there was any."""
    folder = staging_dir(chart_name)
    if not folder.is_dir():
        return False
    shutil.rmtree(folder, ignore_errors=True)
    return True


class StagedPage:
    """One page's staged rows."""

    def __init__(self, name: str, data: dict[str, Any]):
        self.name = name
        self.page_number = data.get("page_number")
        self.width = data.get("width")
        self.height = data.get("height")
        self.fields: dict[str, list[dict[str, Any]]] = data.get("fields") or {}
        # How sure "not found" is, per field with no accepted row (absent_confidences).
        # Staging written before it existed: from the candidates alone.
        self.absent: dict[str, float] = (
            data["absent"] if isinstance(data.get("absent"), dict) else absent_confidences(self.fields)
        )

    def rows(self, field_id: str) -> list[dict[str, Any]]:
        return self.fields.get(field_id) or []

    def accepted(self, field_id: str) -> list[dict[str, Any]]:
        """Every candidate the extraction stands behind, in page order."""
        return [row for row in self.rows(field_id) if row.get("accepted")]

    def selected(self, field_id: str) -> list[dict[str, Any]]:
        """The candidates it chose: one for a single-value field, several for IDs and headings.

        A heading row has no separate choice (it is a heading or it is not), so every accepted
        heading counts.
        """
        return [row for row in self.accepted(field_id) if row.get("selected", True)]

    def candidates(self, field_id: str) -> list[dict[str, Any]]:
        """Every row with a value that the rules accepted (or the rules' pick in older staging)."""
        return [row for row in self.rows(field_id) if row.get("value") and (_rule_accepted(row) or row.get("selected"))]

    def best(self, field_id: str, *, tier: str | None = None) -> Optional[dict[str, Any]]:
        """The candidate the model scored highest, at any score (only rows of ``tier`` when
        given). Without model scores (rules only): the rules' own pick, else their highest
        score."""
        rows = self.candidates(field_id)
        if tier is not None:
            rows = [row for row in rows if row.get("tier") == tier]
        if not rows:
            return None
        scored = [row for row in rows if row.get("model_score") is not None]
        if scored:
            return max(scored, key=lambda row: float(row["model_score"]))
        picked = [row for row in rows if row.get("selected")]
        return picked[0] if picked else max(rows, key=lambda row: float(row.get("score") or 0))


def _rule_accepted(row: dict[str, Any]) -> bool:
    """What the rules decided. Staging written before ``rule_accepted`` existed: the rules
    scored the row (they score only what they accept), and did not hand its value to
    another key (source ``*_shared``)."""
    if "rule_accepted" in row:
        return bool(row["rule_accepted"])
    return float(row.get("score") or 0) > 0 and not str(row.get("source") or "").endswith("_shared")


class Staged:
    """A chart's staged extraction."""

    def __init__(self, data: dict[str, Any]):
        self.data = data
        self.model_version: str = data.get("model_version") or ""
        self._pages = {name: StagedPage(name, page) for name, page in (data.get("pages") or {}).items()}

    def __contains__(self, page_name: str) -> bool:
        return page_name in self._pages

    def __iter__(self) -> Iterator[StagedPage]:
        return iter(self._pages.values())

    def __len__(self) -> int:
        return len(self._pages)

    def page(self, page_name: str) -> Optional[StagedPage]:
        return self._pages.get(page_name)
