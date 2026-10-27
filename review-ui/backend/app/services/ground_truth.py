"""Read page_ground_truth for the landing flag and the imaging comparison."""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Literal

from app.core.schemas import ImagingPageResult, PageGroundTruth, PageSequenceLabel
from app.services.chart_run_batch import database_url_usable
from app.services.db import connection

logger = logging.getLogger("review_ui.ground_truth")

_COLUMNS = """
    page_number, source_page_id,
    member_name, member_dob, dos_from, dos_to,
    encounter_type, page_type, codeable,
    blank_page, junk_page, is_invoice, page_sequence, rotation,
    to_jsonb(page_ground_truth) ->> 'is_visible',
    to_jsonb(page_ground_truth) ->> 'rendering_provider',
    to_jsonb(page_ground_truth) ->> 'provider_signature',
    to_jsonb(page_ground_truth) ->> 'member_id',
    to_jsonb(page_ground_truth) ->> 'provider_credentials',
    to_jsonb(page_ground_truth) ->> 'page_subtype',
    to_jsonb(page_ground_truth) ->> 'provider_specialty',
    to_jsonb(page_ground_truth) ->> 'encounter_label',
    to_jsonb(page_ground_truth) ->> 'tilt',
    to_jsonb(page_ground_truth) ->> 'quality',
    to_jsonb(page_ground_truth) ->> 'mirrored',
    to_jsonb(page_ground_truth) ->> 'printed_or_handwritten',
    to_jsonb(page_ground_truth) ->> 'is_duplicate',
    to_jsonb(page_ground_truth) ->> 'handwritten_pct'
"""

_EMPTY = {"", "na", "n/a", "not found", "not available"}

# Which ground truth the screens show: the latest value, whoever wrote it, or
# each page's latest spreadsheet load with manual edits hidden.
GroundTruthSource = Literal["latest", "bulk_upload"]

# Kept in step with schema/v1.sql and core-pipeline's jobs/ground_truth_load.py.
_ADD_ENTRY_TABLE = """
CREATE TABLE IF NOT EXISTS page_ground_truth_entry (
    id                      BIGSERIAL PRIMARY KEY,
    page_ground_truth_id    BIGINT NOT NULL REFERENCES page_ground_truth(id) ON DELETE CASCADE,
    entry_mode              VARCHAR(20) NOT NULL
                            CHECK (entry_mode IN ('bulk_upload', 'manual_entry')),
    labels                  JSONB NOT NULL DEFAULT '{}'::jsonb,
    source_path             TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_page_ground_truth_entry_page_ground_truth_id_entry_mode
    ON page_ground_truth_entry(page_ground_truth_id, entry_mode);
-- Who saved it (2026-10-19). schema/changes_2026-10-19.sql adds these with their
-- users(id) reference; a database without it still saves, with the id unreferenced.
ALTER TABLE page_ground_truth ADD COLUMN IF NOT EXISTS user_id BIGINT;
ALTER TABLE page_ground_truth_entry ADD COLUMN IF NOT EXISTS user_id BIGINT;
-- The page's encounter type (2026-10-27): encounter_type holds the client sheet's page
-- type. schema/changes_2026-10-27.sql adds it too.
ALTER TABLE page_ground_truth ADD COLUMN IF NOT EXISTS encounter_label TEXT;
-- In v1.sql already; a database made before them still saves.
ALTER TABLE page_ground_truth ADD COLUMN IF NOT EXISTS mirrored TEXT;
ALTER TABLE page_ground_truth ADD COLUMN IF NOT EXISTS printed_or_handwritten TEXT;
ALTER TABLE page_ground_truth ADD COLUMN IF NOT EXISTS is_duplicate TEXT;
ALTER TABLE page_ground_truth ADD COLUMN IF NOT EXISTS handwritten_pct TEXT;
"""

# A page written before the entry table existed gets one entry holding its
# current labels: bulk_upload when a load set its source_path, else
# manual_entry. A loaded page edited by hand since counts as bulk_upload.
_SEED_ENTRIES = """
INSERT INTO page_ground_truth_entry
    (page_ground_truth_id, entry_mode, labels, source_path, created_at, updated_at)
SELECT g.id,
       CASE WHEN g.source_path IS NOT NULL THEN 'bulk_upload' ELSE 'manual_entry' END,
       to_jsonb(g) - 'id' - 'chart_name' - 'page_number' - 'source_page_id'
                   - 'source_path' - 'created_at' - 'updated_at' - 'user_id',
       g.source_path, g.updated_at, g.updated_at
  FROM page_ground_truth g
 WHERE NOT EXISTS (
       SELECT 1 FROM page_ground_truth_entry e WHERE e.page_ground_truth_id = g.id
 )
"""

_entries_ready: set[tuple[str, str]] = set()
_entries_lock = threading.Lock()


def ensure_entries(conn, database_url: str, db_schema: str) -> None:
    """Create page_ground_truth_entry if missing and seed pages that predate it.
    Once per process: every later write appends its own entry."""
    key = (database_url, db_schema)
    if key in _entries_ready:
        return
    with _entries_lock:
        if key in _entries_ready:
            return
        with conn.cursor() as cur:
            cur.execute(_ADD_ENTRY_TABLE)
            cur.execute(_SEED_ENTRIES)
        conn.commit()
        _entries_ready.add(key)


# Each page's newest bulk_upload entry.
_LATEST_BULK = """
SELECT DISTINCT ON (g.id)
       g.chart_name, g.page_number, g.source_page_id, e.labels, e.created_at
  FROM page_ground_truth g
  JOIN page_ground_truth_entry e
    ON e.page_ground_truth_id = g.id AND e.entry_mode = 'bulk_upload'
 WHERE g.chart_name = ANY(%s)
 ORDER BY g.id, e.created_at DESC, e.id DESC
"""


# Codeable, Non Codeable, Non-Codeable, NonCodeable, non_codeable, Codable …
_CODEABLE = re.compile(r"\b(?:(non)[\s_-]*)?code?able\b", re.IGNORECASE)


def spell_codeable(value: str | None) -> str | None:
    """Codable / Non-Codable, however a sheet, an older run, or an older
    ground-truth row spelled it. Blank and NA stay empty."""
    text = clean_label(value)
    if text is None:
        return None
    return _CODEABLE.sub(lambda m: "Non-Codable" if m.group(1) else "Codable", text)


def clean_label(value: str | None) -> str | None:
    """Blank and NA are an empty ground-truth cell."""
    text = (value or "").strip()
    if text.casefold() in _EMPTY:
        return None
    return text


def yes_no_label(value: str | None) -> str | None:
    """Member name, DOB, member id, and provider signature are only Yes or No, or
    Skipped (the pipeline rightly skipped the page; 2026-10-26, user)."""
    text = clean_label(value)
    if text is None:
        return None
    folded = text.casefold()
    if folded == "skipped":
        return "Skipped"
    if folded in {"yes", "y", "true"}:
        return "Yes"
    if folded in {"no", "n", "false"}:
        return "No"
    return None


# is_visible, rendering_provider, provider_signature, member_id, and provider_credentials are read
# through to_jsonb so a database created before the column existed returns
# NULL instead of failing the whole lookup.


LABEL_COLUMNS = (
    "member_name", "member_dob", "member_id", "dos_from", "dos_to",
    "encounter_type", "page_type", "codeable",
    "blank_page", "junk_page", "is_invoice", "page_sequence", "rotation",
    "is_visible", "rendering_provider", "provider_signature", "provider_credentials",
    "page_subtype", "provider_specialty", "encounter_label", "tilt", "quality",
    "mirrored", "printed_or_handwritten", "is_duplicate", "handwritten_pct",
)


def _labelled_row_sql() -> str:
    """True when at least one label cell holds a value, so an all-empty save does not count."""
    empty = ", ".join(f"'{value}'" for value in sorted(_EMPTY))
    cells = " OR ".join(
        f"lower(btrim(COALESCE(to_jsonb(g) ->> '{column}', ''))) NOT IN ({empty})"
        for column in LABEL_COLUMNS
    )
    return f"({cells})"


def charts_with_ground_truth(database_url: str | None, db_schema: str = "public") -> set[str]:
    """Chart folder names with at least one page that has a ground-truth value."""
    if not database_url or not database_url_usable(database_url):
        return set()
    try:
        with connection(database_url, db_schema) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT DISTINCT chart_name FROM page_ground_truth g "
                    f"WHERE {_labelled_row_sql()}"
                )
                return {str(row[0]) for row in cur.fetchall() if row[0]}
    except Exception as exc:
        logger.warning("page_ground_truth chart lookup failed: %s", exc)
        return set()


def _page_id(file_name: str) -> int | None:
    stem = Path(file_name).stem
    if stem.isdigit() and int(stem) > 0:
        return int(stem)
    return None


def ground_truth_by_page(
    database_url: str | None,
    chart_name: str,
    db_schema: str = "public",
) -> dict[int, PageGroundTruth]:
    if not database_url or not database_url_usable(database_url) or not chart_name:
        return {}
    try:
        with connection(database_url, db_schema) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT {_COLUMNS}
                      FROM page_ground_truth
                     WHERE chart_name = %s
                    """,
                    (chart_name,),
                )
                rows = cur.fetchall()
    except Exception as exc:
        logger.warning("page_ground_truth page lookup failed: %s", exc)
        return {}

    out: dict[int, PageGroundTruth] = {}
    for row in rows:
        parsed = _ground_truth_from_row(row)
        out[parsed.pageNumber] = parsed
    return out


def _ground_truth_from_row(row: tuple) -> PageGroundTruth:
    return PageGroundTruth(
        pageNumber=int(row[0]),
        sourcePageId=row[1],
        memberName=row[2],
        memberDob=row[3],
        dosFrom=row[4],
        dosTo=row[5],
        encounterType=row[6],
        pageType=row[7],
        codeable=spell_codeable(row[8]),
        blankPage=row[9],
        junkPage=row[10],
        isInvoice=row[11],
        pageSequence=row[12],
        rotation=row[13],
        isVisible=row[14],
        renderingProvider=row[15] if len(row) > 15 else None,
        providerSignature=row[16] if len(row) > 16 else None,
        memberId=row[17] if len(row) > 17 else None,
        providerCredentials=row[18] if len(row) > 18 else None,
        pageSubtype=row[19] if len(row) > 19 else None,
        specialty=row[20] if len(row) > 20 else None,
        encounterLabel=row[21] if len(row) > 21 else None,
        tilt=row[22] if len(row) > 22 else None,
        quality=row[23] if len(row) > 23 else None,
        mirrored=row[24] if len(row) > 24 else None,
        printedOrHandwritten=row[25] if len(row) > 25 else None,
        isDuplicate=row[26] if len(row) > 26 else None,
        handwrittenPct=row[27] if len(row) > 27 else None,
    )


def _chart_name(folder_id: str) -> str:
    name = (folder_id or "").strip()
    if not name or "/" in name or "\\" in name or name in {".", ".."}:
        raise ValueError("invalid chart name")
    return name


_UPSERT = """
WITH g AS (
INSERT INTO page_ground_truth (
    chart_name, page_number, source_page_id,
    member_name, member_dob, member_id, dos_from, dos_to,
    encounter_type, page_type, codeable,
    blank_page, junk_page, is_invoice,
    rotation, is_visible,
    rendering_provider, provider_signature, provider_credentials,
    page_subtype, provider_specialty,
    encounter_label, tilt, quality,
    mirrored, printed_or_handwritten, is_duplicate, handwritten_pct,
    user_id
) VALUES (
    %s, %s, %s,
    %s, %s, %s, %s, %s,
    %s, %s, %s,
    %s, %s, %s,
    %s, %s,
    %s, %s, %s,
    %s, %s,
    %s, %s, %s,
    %s, %s, %s, %s,
    %s
)
ON CONFLICT (chart_name, page_number) DO UPDATE SET
    source_page_id = COALESCE(EXCLUDED.source_page_id, page_ground_truth.source_page_id),
    member_name = EXCLUDED.member_name,
    member_dob = EXCLUDED.member_dob,
    member_id = EXCLUDED.member_id,
    dos_from = EXCLUDED.dos_from,
    dos_to = EXCLUDED.dos_to,
    encounter_type = EXCLUDED.encounter_type,
    page_type = EXCLUDED.page_type,
    codeable = EXCLUDED.codeable,
    blank_page = EXCLUDED.blank_page,
    junk_page = EXCLUDED.junk_page,
    is_invoice = EXCLUDED.is_invoice,
    rotation = EXCLUDED.rotation,
    is_visible = EXCLUDED.is_visible,
    rendering_provider = EXCLUDED.rendering_provider,
    provider_signature = EXCLUDED.provider_signature,
    provider_credentials = EXCLUDED.provider_credentials,
    page_subtype = EXCLUDED.page_subtype,
    provider_specialty = EXCLUDED.provider_specialty,
    encounter_label = EXCLUDED.encounter_label,
    tilt = EXCLUDED.tilt,
    quality = EXCLUDED.quality,
    mirrored = EXCLUDED.mirrored,
    printed_or_handwritten = EXCLUDED.printed_or_handwritten,
    is_duplicate = EXCLUDED.is_duplicate,
    handwritten_pct = EXCLUDED.handwritten_pct,
    user_id = EXCLUDED.user_id,
    updated_at = now()
RETURNING
    id, page_number, source_page_id,
    member_name, member_dob, dos_from, dos_to,
    encounter_type, page_type, codeable,
    blank_page, junk_page, is_invoice, page_sequence, rotation,
    is_visible, rendering_provider, provider_signature,
    member_id, provider_credentials, page_subtype, provider_specialty,
    encounter_label, tilt, quality,
    mirrored, printed_or_handwritten, is_duplicate, handwritten_pct
), e AS (
INSERT INTO page_ground_truth_entry (page_ground_truth_id, entry_mode, labels, user_id)
SELECT id, 'manual_entry', %s::jsonb, %s FROM g
)
SELECT
    page_number, source_page_id,
    member_name, member_dob, dos_from, dos_to,
    encounter_type, page_type, codeable,
    blank_page, junk_page, is_invoice, page_sequence, rotation,
    is_visible, rendering_provider, provider_signature,
    member_id, provider_credentials, page_subtype, provider_specialty,
    encounter_label, tilt, quality,
    mirrored, printed_or_handwritten, is_duplicate, handwritten_pct
  FROM g
"""

# The page_ground_truth columns a manual save sets, in _UPSERT's parameter order.
_MANUAL_COLUMNS = (
    "member_name", "member_dob", "member_id", "dos_from", "dos_to",
    "encounter_type", "page_type", "codeable",
    "blank_page", "junk_page", "is_invoice",
    "rotation", "is_visible",
    "rendering_provider", "provider_signature", "provider_credentials",
    "page_subtype", "provider_specialty",
    "encounter_label", "tilt", "quality",
    "mirrored", "printed_or_handwritten", "is_duplicate", "handwritten_pct",
)


def upsert_page_ground_truth(
    database_url: str,
    folder_id: str,
    body: PageGroundTruth,
    db_schema: str = "public",
    *,
    user_id: int | None = None,
) -> PageGroundTruth:
    """Insert or update the ground-truth row for one chart page.

    A later manual save of the same page hits the same
    ``(chart_name, page_number)`` key. Columns this screen does not edit
    (page sequence, deleted level, source path) are left as they are.
    The save is also appended to page_ground_truth_entry as a manual_entry. ``user_id``
    (the signed-in user) is stored on both.
    """
    chart = _chart_name(folder_id)
    if body.pageNumber < 1:
        raise ValueError("invalid page number")
    params = (
        chart,
        body.pageNumber,
        clean_label(body.sourcePageId),
        yes_no_label(body.memberName),
        yes_no_label(body.memberDob),
        yes_no_label(body.memberId),
        clean_label(body.dosFrom),
        clean_label(body.dosTo),
        clean_label(body.encounterType),
        clean_label(body.pageType),
        spell_codeable(body.codeable),
        clean_label(body.blankPage),
        clean_label(body.junkPage),
        clean_label(body.isInvoice),
        clean_label(body.rotation),
        clean_label(body.isVisible),
        clean_label(body.renderingProvider),
        yes_no_label(body.providerSignature),
        clean_label(body.providerCredentials),
        clean_label(body.pageSubtype),
        clean_label(body.specialty),
        clean_label(body.encounterLabel),
        clean_label(body.tilt),
        clean_label(body.quality),
        clean_label(body.mirrored),
        clean_label(body.printedOrHandwritten),
        clean_label(body.isDuplicate),
        clean_label(body.handwrittenPct),
    )
    labels = json.dumps(dict(zip(_MANUAL_COLUMNS, params[3:])))
    with connection(database_url, db_schema) as conn:
        ensure_entries(conn, database_url, db_schema)
        with conn.cursor() as cur:
            cur.execute(_UPSERT, (*params, user_id, labels, user_id))
            row = cur.fetchone()
    if row is None:
        raise RuntimeError("ground truth upsert returned no row")
    return _ground_truth_from_row(row)


_UPSERT_SEQUENCE = """
WITH g AS (
INSERT INTO page_ground_truth (chart_name, page_number, source_page_id, page_sequence, user_id)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (chart_name, page_number) DO UPDATE SET
    source_page_id = COALESCE(EXCLUDED.source_page_id, page_ground_truth.source_page_id),
    page_sequence = EXCLUDED.page_sequence,
    user_id = EXCLUDED.user_id,
    updated_at = now()
RETURNING id
)
INSERT INTO page_ground_truth_entry (page_ground_truth_id, entry_mode, labels, user_id)
SELECT id, 'manual_entry', %s::jsonb, %s FROM g
"""


# Main pages 1, 2, 3 …; blank / junk pages J1, J2, J3 ….
_SEQUENCE = re.compile(r"J?[1-9]\d*")


def save_page_sequences(
    database_url: str,
    folder_id: str,
    pages: list[PageSequenceLabel],
    db_schema: str = "public",
    *,
    user_id: int | None = None,
) -> dict[int, PageGroundTruth]:
    """Set the ground-truth actual sequence of each page in ``pages``; other labels on
    those pages are left as they are. Each page's change is a manual_entry.
    Returns every ground-truth page of the chart, by page number."""
    chart = _chart_name(folder_id)
    for page in pages:
        if page.pageNumber < 1:
            raise ValueError("invalid page number")
        value = clean_label(page.pageSequence)
        if value is not None and not _SEQUENCE.fullmatch(value):
            raise ValueError(
                f"page {page.pageNumber}: sequence must be a whole number from 1, or J1, J2 … for blank / junk"
            )
    with connection(database_url, db_schema) as conn:
        ensure_entries(conn, database_url, db_schema)
        with conn.cursor() as cur:
            for page in pages:
                value = clean_label(page.pageSequence)
                cur.execute(
                    _UPSERT_SEQUENCE,
                    (
                        chart, page.pageNumber, clean_label(page.sourcePageId), value, user_id,
                        json.dumps({"page_sequence": value}), user_id,
                    ),
                )
    return ground_truth_by_page(database_url, chart, db_schema)


def _ground_truth_from_labels(
    page_number: int, source_page_id: str | None, labels: dict
) -> PageGroundTruth:
    def get(column: str) -> str | None:
        value = labels.get(column)
        return None if value is None else str(value)

    return PageGroundTruth(
        pageNumber=int(page_number),
        sourcePageId=source_page_id,
        memberName=get("member_name"),
        memberDob=get("member_dob"),
        memberId=get("member_id"),
        dosFrom=get("dos_from"),
        dosTo=get("dos_to"),
        encounterType=get("encounter_type"),
        pageType=get("page_type"),
        codeable=spell_codeable(get("codeable")),
        blankPage=get("blank_page"),
        junkPage=get("junk_page"),
        isInvoice=get("is_invoice"),
        pageSequence=get("page_sequence"),
        rotation=get("rotation"),
        isVisible=get("is_visible"),
        renderingProvider=get("rendering_provider"),
        providerSignature=get("provider_signature"),
        providerCredentials=get("provider_credentials"),
        pageSubtype=get("page_subtype"),
        specialty=get("provider_specialty"),
        encounterLabel=get("encounter_label"),
        tilt=get("tilt"),
        quality=get("quality"),
        mirrored=get("mirrored"),
        printedOrHandwritten=get("printed_or_handwritten"),
        isDuplicate=get("is_duplicate"),
        handwrittenPct=get("handwritten_pct"),
    )


def latest_bulk_labels(
    conn, database_url: str, chart_names: list[str], db_schema: str = "public"
) -> dict[tuple[str, int], tuple[dict, datetime | None]]:
    """(chart, page number) -> (labels, loaded at) from each page's newest
    bulk_upload entry. A page only ever edited by hand is absent."""
    ensure_entries(conn, database_url, db_schema)
    with conn.cursor() as cur:
        cur.execute(_LATEST_BULK, (chart_names,))
        rows = cur.fetchall()
    out: dict[tuple[str, int], tuple[dict, datetime | None]] = {}
    for chart, page_number, source_page_id, labels, created_at in rows:
        if isinstance(labels, str):
            labels = json.loads(labels)
        out[(str(chart), int(page_number))] = (
            {**(labels or {}), "source_page_id": source_page_id},
            created_at,
        )
    return out


def ground_truth_charts(database_url: str, db_schema: str = "public") -> list[str]:
    """Every chart with at least one ground-truth page."""
    with connection(database_url, db_schema) as conn, conn.cursor() as cur:
        cur.execute("SELECT DISTINCT chart_name FROM page_ground_truth ORDER BY chart_name")
        return [str(row[0]) for row in cur.fetchall()]


def ground_truth_labels(
    database_url: str,
    chart_names: list[str],
    source: GroundTruthSource = "latest",
    db_schema: str = "public",
) -> list[tuple[str, int, dict]]:
    """(chart, page number, labels keyed by page_ground_truth column) for the
    given charts: the latest values, or each page's latest bulk upload."""
    with connection(database_url, db_schema) as conn:
        if source == "bulk_upload":
            bulk = latest_bulk_labels(conn, database_url, chart_names, db_schema)
            return [
                (chart, page_number, labels)
                for (chart, page_number), (labels, _loaded_at) in sorted(bulk.items())
            ]
        with conn.cursor() as cur:
            cur.execute(
                "SELECT chart_name, page_number, to_jsonb(g) FROM page_ground_truth g "
                "WHERE chart_name = ANY(%s) ORDER BY chart_name, page_number",
                (chart_names,),
            )
            rows = cur.fetchall()
    out: list[tuple[str, int, dict]] = []
    for chart, page_number, labels in rows:
        if isinstance(labels, str):
            labels = json.loads(labels)
        out.append((str(chart), int(page_number), labels or {}))
    return out


def bulk_ground_truth(
    pages: list[ImagingPageResult],
    chart_name: str,
    database_url: str | None,
    db_schema: str = "public",
) -> list[ImagingPageResult]:
    """Each page's ground truth as of its latest bulk upload; a page with no
    bulk upload shows none, so manual edits are hidden."""
    if not database_url or not database_url_usable(database_url) or not chart_name:
        return pages
    try:
        with connection(database_url, db_schema) as conn:
            bulk = latest_bulk_labels(conn, database_url, [chart_name], db_schema)
    except Exception as exc:
        logger.warning("page_ground_truth_entry lookup failed: %s", exc)
        return pages
    out: list[ImagingPageResult] = []
    for page in pages:
        number = _page_id(page.fileName)
        hit = bulk.get((chart_name, number)) if number is not None else None
        truth = (
            _ground_truth_from_labels(number, hit[0].get("source_page_id"), hit[0])
            if hit
            else None
        )
        out.append(page.model_copy(update={"groundTruth": truth}))
    return out


def attach_ground_truth(
    pages: list[ImagingPageResult],
    chart_name: str,
    database_url: str | None,
    db_schema: str = "public",
) -> list[ImagingPageResult]:
    """Match each page file stem (1.jpg / 1.png / 1.tif) to ground-truth Id."""
    by_page = ground_truth_by_page(database_url, chart_name, db_schema)
    if not by_page:
        return pages
    attached: list[ImagingPageResult] = []
    for page in pages:
        number = _page_id(page.fileName)
        hit = by_page.get(number) if number is not None else None
        if hit is None:
            attached.append(page)
        else:
            attached.append(page.model_copy(update={"groundTruth": hit}))
    return attached
