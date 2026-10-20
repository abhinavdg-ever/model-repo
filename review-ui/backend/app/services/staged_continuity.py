"""The encounter grouping the pipeline stages in ``<chart>/staging/continuity.json``.

The ``encounter_type`` stage appends ``encounters`` (one entry per encounter: its pages,
page family, DOS and the one decision its pages carry). Doc Summary builds from it; a
chart without it (older runs, Production Mode without the workspace) falls back to
deriving encounters from ``document_seq``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from app.core.schemas import StagedEncounter

logger = logging.getLogger(__name__)


def load_staged_encounters(data_root: Path | None, folder_id: str) -> list[StagedEncounter]:
    """The staged encounters, or [] when the chart has none (never raises)."""
    if data_root is None or "/" in folder_id or "\\" in folder_id or folder_id in (".", ".."):
        return []
    path = data_root / folder_id / "staging" / "continuity.json"
    try:
        if not path.is_file():
            return []
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("continuity.json unreadable for %s: %s", folder_id, exc)
        return []
    out: list[StagedEncounter] = []
    for raw in payload.get("encounters") or []:
        try:
            out.append(StagedEncounter.model_validate(raw))
        except ValueError:
            continue
    return out
