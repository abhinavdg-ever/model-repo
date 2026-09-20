"""Stage: section-header match from on-disk Final1 / Final2 OCR JSON.

Reads ``ocr/<chart>_final1.json`` and ``*_final2.json``, derives header
candidates from stored structure (``section_header_candidates``, Azure
``pagesMeta`` lines, or Docling ``document``), filters against
``section_header_canon.json``, and writes ``section_headers`` back — no OCR.

Re-run after editing the canon list or matcher:

    curl -X POST .../api/charts/run -d '{"chart_id": N, "only": ["section_headers"]}'
    python cli.py run --chart-id N --only section_headers
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from db import connect, get_ocr_texts, upsert_ocr_result
from db.paths import ocr_dir, write_final1_json, write_final2_json
from stages._support import (
    mark_completed,
    mark_failed,
    mark_processing,
    mark_skipped,
    stage_run,
)
from stages.lib.imaging.section_headers_io import (
    page_has_ocr_payload,
    refresh_ocr_json_doc,
    refresh_page_headers,
)

logger = logging.getLogger(__name__)

STAGE = "section_headers"


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Could not read %s: %s", path, exc)
        return None
    return data if isinstance(data, dict) else None


def _pages_by_name(doc: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not doc:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for page in doc.get("pages") or []:
        if not isinstance(page, dict):
            continue
        name = str(page.get("fileName") or "").strip()
        if name:
            out[name] = page
    return out


def _sync_db_page(
    conn: Any,
    *,
    chart_id: int,
    page_id: int,
    ocr_type: str,
    page_doc: dict[str, Any],
) -> None:
    """Merge refreshed headers into the stored ``ocr_results.raw_text`` JSON."""
    stored = get_ocr_texts(conn, chart_id, ocr_type).get(page_id)
    base: dict[str, Any] = {}
    if stored:
        try:
            parsed = json.loads(stored)
            if isinstance(parsed, dict):
                base = parsed
        except (ValueError, TypeError):
            base = {"content": str(stored)}
    base.update(
        {
            "section_header_candidates": page_doc.get("section_header_candidates")
            or [],
            "section_headers": page_doc.get("section_headers") or [],
        }
    )
    for key in (
        "pageNumber",
        "fileName",
        "content",
        "markdown",
        "pagesMeta",
        "engine",
        "languages",
        "features",
        "document",
        "skippedReason",
    ):
        if key in page_doc and page_doc[key] is not None:
            base[key] = page_doc[key]
    upsert_ocr_result(
        conn,
        chart_id=chart_id,
        page_id=page_id,
        ocr_type=ocr_type,
        raw_text=json.dumps(base, default=str),
    )


def run(chart_id: int, *, force: bool = False) -> dict[str, Any]:
    with stage_run(chart_id, STAGE, force=force) as ctx:
        final1_path = ocr_dir(ctx.chart_name) / f"{ctx.chart_name}_final1.json"
        final2_path = ocr_dir(ctx.chart_name) / f"{ctx.chart_name}_final2.json"
        final1_doc = _load_json(final1_path)
        final2_doc = _load_json(final2_path)

        if final1_doc is None and final2_doc is None:
            with connect() as conn:
                mark_skipped(
                    conn,
                    ctx,
                    sorted(ctx.todo),
                    "no_final_ocr_json",
                )
            logger.warning(
                "Section headers: no %s or %s — nothing to derive",
                final1_path.name,
                final2_path.name,
            )
            return {
                "chart_id": chart_id,
                "final1_path": str(final1_path),
                "final2_path": str(final2_path),
                "pages_done": ctx.done,
                "skipped": ctx.skipped,
                "errors": ctx.errors,
                "headers_kept": 0,
            }

        # Prefer Final2 when both exist (richer line geometry); always refresh
        # both files so Local Mode overlays stay consistent.
        f1_by_name = _pages_by_name(final1_doc)
        f2_by_name = _pages_by_name(final2_doc)

        todo_pages = [p for p in ctx.pages if p["id"] in ctx.todo]
        with connect() as conn:
            for page in todo_pages:
                mark_processing(conn, ctx, page["id"])

        headers_kept = 0
        touched_final1 = False
        touched_final2 = False

        with connect() as conn:
            for page in todo_pages:
                name = page["page_name"]
                try:
                    f2_page = f2_by_name.get(name)
                    f1_page = f1_by_name.get(name)
                    # Prefer Azure when it has lines / candidates; else Final1.
                    source_page = None
                    kind = ""
                    ocr_type = ""
                    if f2_page is not None and page_has_ocr_payload(f2_page):
                        # HQ-printed Final2 stubs are empty — fall through.
                        if not str(f2_page.get("skippedReason") or "").strip() or (
                            f2_page.get("pagesMeta")
                            or f2_page.get("section_header_candidates")
                        ):
                            source_page = f2_page
                            kind = "final2"
                            ocr_type = "azuredocintel"
                    if source_page is None and f1_page is not None and page_has_ocr_payload(
                        f1_page
                    ):
                        source_page = f1_page
                        kind = "final1"
                        ocr_type = "docling"
                    if source_page is None:
                        mark_skipped(conn, ctx, [page["id"]], "no_ocr_payload")
                        continue

                    refreshed = refresh_page_headers(
                        source_page, kind=kind, use_minilm=True
                    )
                    headers_kept += len(refreshed.get("section_headers") or [])

                    if kind == "final2" and name in f2_by_name:
                        f2_by_name[name] = refreshed
                        touched_final2 = True
                        # Mirror filtered headers onto Final1 page when present
                        # so Local Mode (often Final1) sees the same overlays.
                        if name in f1_by_name:
                            mirrored = dict(f1_by_name[name])
                            mirrored["section_header_candidates"] = refreshed.get(
                                "section_header_candidates"
                            ) or []
                            mirrored["section_headers"] = refreshed.get(
                                "section_headers"
                            ) or []
                            f1_by_name[name] = mirrored
                            touched_final1 = True
                        _sync_db_page(
                            conn,
                            chart_id=chart_id,
                            page_id=page["id"],
                            ocr_type=ocr_type,
                            page_doc=refreshed,
                        )
                        if name in f1_by_name:
                            _sync_db_page(
                                conn,
                                chart_id=chart_id,
                                page_id=page["id"],
                                ocr_type="docling",
                                page_doc=f1_by_name[name],
                            )
                    else:
                        f1_by_name[name] = refreshed
                        touched_final1 = True
                        _sync_db_page(
                            conn,
                            chart_id=chart_id,
                            page_id=page["id"],
                            ocr_type=ocr_type or "docling",
                            page_doc=refreshed,
                        )

                    mark_completed(conn, ctx, page["id"])
                except Exception as exc:
                    logger.exception(
                        "Section headers failed for %s", page["page_name"]
                    )
                    mark_failed(
                        conn, ctx, page["id"], str(exc), page["page_name"]
                    )

        # Rewrite JSON files from the refreshed page maps (full chart).
        if touched_final1 or final1_doc is not None:
            model = (final1_doc or {}).get("model") or "docling+rapidocr"
            # Keep pages that were not in todo unchanged from the original doc.
            if final1_doc is not None:
                pages = []
                for page in final1_doc.get("pages") or []:
                    if not isinstance(page, dict):
                        continue
                    name = str(page.get("fileName") or "")
                    pages.append(f1_by_name.get(name, page))
                write_final1_json(ctx.chart_name, pages, model=str(model))
            elif f1_by_name:
                write_final1_json(
                    ctx.chart_name, list(f1_by_name.values()), model=str(model)
                )

        if touched_final2 or final2_doc is not None:
            if final2_doc is not None:
                pages = []
                for page in final2_doc.get("pages") or []:
                    if not isinstance(page, dict):
                        continue
                    name = str(page.get("fileName") or "")
                    pages.append(f2_by_name.get(name, page))
                write_final2_json(ctx.chart_name, pages)
            elif f2_by_name:
                write_final2_json(ctx.chart_name, list(f2_by_name.values()))

        # If we only had disk JSON and refreshed via refresh_ocr_json_doc path
        # for pages outside todo — already handled page-by-page above.

        logger.info(
            "Section headers: %d page(s), %d header(s) kept (final1=%s final2=%s)",
            ctx.done,
            headers_kept,
            final1_path.name if final1_path.is_file() else "missing",
            final2_path.name if final2_path.is_file() else "missing",
        )
        return {
            "chart_id": chart_id,
            "final1_path": str(final1_path),
            "final2_path": str(final2_path),
            "pages_done": ctx.done,
            "skipped": ctx.skipped,
            "errors": ctx.errors,
            "headers_kept": headers_kept,
        }


def refresh_chart_json_files(chart_name: str, *, use_minilm: bool = True) -> dict[str, Any]:
    """Offline helper: rewrite both OCR JSONs for a chart folder (no DB)."""
    root = ocr_dir(chart_name)
    summary: dict[str, Any] = {"chart_name": chart_name, "files": {}}
    for kind, name, writer in (
        ("final1", f"{chart_name}_final1.json", write_final1_json),
        ("final2", f"{chart_name}_final2.json", write_final2_json),
    ):
        path = root / name
        doc = _load_json(path)
        if doc is None:
            summary["files"][kind] = {"path": str(path), "status": "missing"}
            continue
        new_doc, n_pages, n_headers = refresh_ocr_json_doc(
            doc, kind=kind, use_minilm=use_minilm
        )
        if kind == "final1":
            writer(chart_name, list(new_doc.get("pages") or []), model=str(new_doc.get("model") or "docling+rapidocr"))
        else:
            writer(chart_name, list(new_doc.get("pages") or []))
        summary["files"][kind] = {
            "path": str(path),
            "status": "updated",
            "pages": n_pages,
            "headers_kept": n_headers,
        }
    return summary
