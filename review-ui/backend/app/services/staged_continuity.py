"""The encounter grouping the pipeline stages in ``<chart>/staging/continuity.json``.

The ``encounter_type`` stage appends ``encounters`` (one entry per encounter: its pages,
page family, DOS and the one decision its pages carry). Doc Summary builds from it; a
chart without it (older runs, Production Mode without the workspace) falls back to
deriving encounters from ``document_seq``. Production Mode reads the same grouping from
``encounter_type_results`` (:func:`load_encounters_from_db`).
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


# Each encounter from its pages' encounter_type_results rows (every page of an encounter
# carries the same decision): the pages in order, the first page's document, family and DOS.
_ENCOUNTERS_SQL = """
    SELECT e.encounter_seq,
           array_agg(p.page_name ORDER BY p.page_number, p.id) AS pages,
           (array_agg(to_jsonb(e) ORDER BY p.page_number, p.id))[1] AS decision,
           (array_agg(k.document_seq ORDER BY p.page_number, p.id))[1] AS document_seq,
           (array_agg(pc.axes ->> 'l1' ORDER BY p.page_number, p.id)
               FILTER (WHERE coalesce(pc.axes ->> 'l1', '') NOT IN ('', 'Unknown')))[1] AS page_family,
           (array_agg(d.date_of_service_from ORDER BY p.page_number, p.id))[1] AS dos_from,
           (array_agg(d.date_of_service_to ORDER BY p.page_number, p.id))[1] AS dos_to
    FROM encounter_type_results e
    JOIN page_list p ON p.id = e.page_id
    JOIN chart_list c ON c.id = e.chart_id
    LEFT JOIN page_continuity_results k ON k.page_id = e.page_id
    LEFT JOIN page_classification pc ON pc.page_id = e.page_id
    LEFT JOIN dos_extraction_results d ON d.page_id = e.page_id
    WHERE c.chart_name = %s AND e.encounter_seq IS NOT NULL
    GROUP BY e.encounter_seq
    ORDER BY e.encounter_seq
"""

# encounter_type_results.encounter_type -> the label review-ui shows.
_LABELS = {
    "outpatient_f2f": "Outpatient (F2F)",
    "outpatient_tele": "Outpatient (Tele)",
    "inpatient": "Inpatient",
    "home": "Home",
    "not_applicable": "Not Available",
}


def load_encounters_from_db(database_url: str, db_schema: str, folder_id: str) -> list[StagedEncounter]:
    """Production Mode: the encounter grouping from ``encounter_type_results`` (its
    ``encounter_seq`` and decision columns); [] when the columns are missing or the
    database cannot be read (logged, never raises)."""
    from app.services.db import connection

    try:
        with connection(database_url, db_schema) as conn:
            with conn.cursor() as cur:
                cur.execute(_ENCOUNTERS_SQL, (folder_id,))
                rows = cur.fetchall()
    except Exception as exc:
        logger.warning("encounter grouping unreadable for %s: %s", folder_id, exc)
        return []
    out: list[StagedEncounter] = []
    for seq, pages, decision, document, family, dos_from, dos_to in rows:
        decision = decision if isinstance(decision, dict) else {}
        setting = str(decision.get("encounter_type") or "")
        try:
            out.append(StagedEncounter(
                seq=int(seq),
                document=document,
                page_family=family,
                pages=[str(name) for name in pages or []],
                dos_from=str(dos_from) if dos_from else None,
                dos_to=str(dos_to) if dos_to else None,
                encounter_type=setting or None,
                encounter_label=_LABELS.get(setting, setting) or None,
                confidence=decision.get("confidence"),
                decided_by=decision.get("decided_by"),
                reason=decision.get("reason"),
                matched_keyword=decision.get("matched_keyword"),
            ))
        except ValueError:
            continue
    return out
