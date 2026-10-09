"""Rules applied after the family and subtype are chosen.

Each rule rewrites a page in place. Add the next one to ``apply`` in order.
"""
from __future__ import annotations

import csv
import logging
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

_PROGRESS = "progress_note"
_DEMOGRAPHICS = "patient_demographics"


def signed_page_names(chart_name: str) -> set[str]:
    """Page names whose extraction row says a signature is present."""
    from db.paths import imaging_csv

    path = imaging_csv(chart_name, "provider_signature")
    if not path.is_file():
        return set()
    names: set[str] = set()
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if str(row.get("signature_present") or "").strip().casefold() == "y":
                name = str(row.get("page_name") or "").strip()
                if name:
                    names.add(name)
    return names


def apply(
    rows: list[dict[str, Any]], *, signed_names: Optional[Iterable[str]] = None
) -> list[dict[str, Any]]:
    """Rewrite ``rows`` with the rules added so far. Returns the same list."""
    signed = {str(name) for name in (signed_names or ())}
    _signature_is_progress_note(rows, signed)
    _demographics_cannot_split_progress_notes(rows)
    return rows


def _is_progress(row: dict[str, Any]) -> bool:
    return str(row.get("family") or "") == _PROGRESS


def _is_demographics(row: dict[str, Any]) -> bool:
    return str(row.get("family") or "") == _DEMOGRAPHICS


def _as_progress(row: dict[str, Any], source: str, confidence: Any) -> None:
    row.update(
        {
            "page_type": "Progress Note",
            "tag": "codeable",
            "is_codeable": "Codeable",
            "confidence": confidence if confidence not in ("", None) else row.get("confidence") or "",
            "continue": "y",
            "continue_applied": "y" if source == "between" else "n",
            "family": _PROGRESS,
            "family_display": "Progress Note",
            "family_source": source,
            "entry_id": "progress_note",
            "type_confidence": row.get("type_confidence") or 1.0,
        }
    )


def _signature_is_progress_note(
    rows: list[dict[str, Any]], signed: set[str]
) -> None:
    """A page with a signature is a Progress Note."""
    if not signed:
        return
    changed = 0
    for row in rows:
        name = str(row.get("page_name") or "")
        if name not in signed or _is_progress(row):
            continue
        _as_progress(row, "signature", row.get("confidence"))
        changed += 1
    if changed:
        logger.info("Post-process: %d signed page(s) set to Progress Note", changed)


def _demographics_cannot_split_progress_notes(rows: list[dict[str, Any]]) -> None:
    """A run of demographics with a Progress Note on both sides becomes one too."""
    changed = 0
    index = 0
    while index < len(rows):
        if not _is_progress(rows[index]):
            index += 1
            continue
        end = index + 1
        while end < len(rows) and _is_demographics(rows[end]):
            end += 1
        if end > index + 1 and end < len(rows) and _is_progress(rows[end]):
            bounds = [
                c
                for c in (rows[index].get("confidence"), rows[end].get("confidence"))
                if c not in ("", None)
            ]
            confidence = min(bounds) if bounds else ""
            for row in rows[index + 1 : end]:
                _as_progress(row, "between", confidence)
                changed += 1
        index = end if end > index else index + 1
    if changed:
        logger.info(
            "Post-process: %d demographics page(s) between Progress Notes rewritten",
            changed,
        )
