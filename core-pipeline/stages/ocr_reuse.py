"""Reuse OCR artifacts when ``SKIP_OCR`` / ``skip_ocr`` is set.

Priority when skipping OCR engines:

  1. **Output folder** ``ocr/`` (``chart_list.output_path`` — local path or
     blob prefix under the chart's container). Files are copied into the
     workspace ``ocr/`` then hydrated into ``ocr_results``.
  2. Workspace ``data/folders/<chart>/ocr/`` (same hydrate).
  3. Else ``ocr_results`` in Postgres → write the three workspace files
     (``*_prelim.txt``, ``*_final1.json``, ``*_final2.json``).
  4. Else run OCR normally.

``force=True`` always runs OCR.
"""
from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any, Optional

from db import connect, get_ocr_texts, list_pages, set_pages_stage, upsert_ocr_result
from db.paths import (
    ocr_dir,
    parse_combined_ocr_txt,
    parse_ocr_json,
    write_combined_ocr_txt,
    write_final1_json,
    write_final2_json,
)

logger = logging.getLogger(__name__)

OCR_STAGE_NAMES = ("ocr_prelim", "ocr_final1", "ocr_final2")

_OCR_SUFFIXES = (
    "_prelim.txt",
    "_final1.json",
    "_final2.json",
    "_final1.txt",  # legacy
)


def _ocr_filenames(chart_name: str) -> list[str]:
    return [f"{chart_name}{suffix}" for suffix in _OCR_SUFFIXES]


def ocr_artifacts_in_dir(root: Path, chart_name: str) -> bool:
    """True when ``root`` holds at least one usable OCR artifact for the chart."""
    if not root.is_dir():
        return False
    prelim = root / f"{chart_name}_prelim.txt"
    if prelim.is_file() and prelim.stat().st_size > 0:
        return True
    for name in (f"{chart_name}_final1.json", f"{chart_name}_final2.json"):
        path = root / name
        if not path.is_file() or path.stat().st_size == 0:
            continue
        try:
            if parse_ocr_json(path):
                return True
        except (ValueError, TypeError, json.JSONDecodeError):
            continue
    return False


def ocr_artifacts_present(chart_name: str) -> bool:
    """True when the chart's **workspace** ``ocr/`` has usable files."""
    return ocr_artifacts_in_dir(ocr_dir(chart_name), chart_name)


def _chart_row(chart_id: int) -> Optional[dict[str, Any]]:
    with connect() as conn:
        return conn.execute(
            """
            SELECT id, chart_name, output_path, blob_container, source
              FROM chart_list WHERE id = %s
            """,
            (chart_id,),
        ).fetchone()


def _local_output_ocr_candidates(output_path: str, chart_name: str) -> list[Path]:
    """Possible on-disk ocr/ directories derived from ``output_path``."""
    raw = (output_path or "").strip().rstrip("/\\")
    if not raw:
        return []
    base = Path(raw)
    # output_path is usually …/<chart_name>; also accept a bare prefix.
    candidates = [
        base / "ocr",
        base,
    ]
    if base.name != chart_name:
        candidates.insert(0, base / chart_name / "ocr")
        candidates.insert(1, base / chart_name)
    # Deduplicate while preserving order.
    seen: set[str] = set()
    out: list[Path] = []
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out


def _copy_ocr_dir_into_workspace(src: Path, chart_name: str) -> int:
    """Copy known OCR files from ``src`` into the workspace ``ocr/``. Returns count."""
    dest = ocr_dir(chart_name)
    dest.mkdir(parents=True, exist_ok=True)
    copied = 0
    for name in _ocr_filenames(chart_name):
        src_file = src / name
        if not src_file.is_file() or src_file.stat().st_size == 0:
            continue
        target = dest / name
        if src_file.resolve() == target.resolve():
            copied += 1
            continue
        shutil.copy2(src_file, target)
        copied += 1
    return copied


def _pull_ocr_from_blob_output(
    *,
    container: str,
    output_path: str,
    chart_name: str,
) -> int:
    """Download OCR artifacts from ``{output_path}/ocr/`` (or the folder itself)."""
    from db.blob_store import download_blob_to_path, list_blobs_with_suffixes

    prefix = output_path.strip().strip("/").replace("\\", "/")
    # Prefer …/ocr/ under the chart output; also accept files sitting next to it.
    prefixes = [f"{prefix}/ocr", prefix]
    dest = ocr_dir(chart_name)
    dest.mkdir(parents=True, exist_ok=True)
    copied = 0
    wanted = set(_ocr_filenames(chart_name))
    for pref in prefixes:
        try:
            names = list_blobs_with_suffixes(
                container, pref, (".txt", ".json")
            )
        except Exception as exc:
            logger.warning(
                "SKIP_OCR: could not list blob ocr under %s/%s (%s)",
                container,
                pref,
                exc,
            )
            continue
        for blob_name in names:
            filename = Path(blob_name).name
            if filename not in wanted or filename.startswith("._"):
                continue
            try:
                download_blob_to_path(container, blob_name, dest / filename)
                copied += 1
            except Exception as exc:
                logger.warning(
                    "SKIP_OCR: failed to download %s (%s)", blob_name, exc
                )
        if copied:
            break
    return copied


def sync_ocr_from_output_folder(chart_id: int, chart_name: str) -> Optional[str]:
    """Bring output-folder OCR into the workspace. Returns source label or None."""
    row = _chart_row(chart_id)
    if not row:
        return None
    output_path = (row.get("output_path") or "").strip()
    if not output_path:
        return None

    for candidate in _local_output_ocr_candidates(output_path, chart_name):
        if ocr_artifacts_in_dir(candidate, chart_name):
            n = _copy_ocr_dir_into_workspace(candidate, chart_name)
            if n:
                logger.info(
                    "SKIP_OCR: copied %d OCR file(s) from output folder %s → workspace",
                    n,
                    candidate,
                )
                return f"output:{candidate}"

    container = (row.get("blob_container") or "").strip()
    if container:
        n = _pull_ocr_from_blob_output(
            container=container,
            output_path=output_path,
            chart_name=chart_name,
        )
        if n and ocr_artifacts_present(chart_name):
            logger.info(
                "SKIP_OCR: pulled %d OCR file(s) from blob %s/%s → workspace",
                n,
                container,
                output_path,
            )
            return f"blob:{container}/{output_path}"

    return None


def output_or_workspace_ocr_ready(chart_id: int, chart_name: str) -> bool:
    """True if output-folder or workspace OCR can be used for skip_ocr."""
    if ocr_artifacts_present(chart_name):
        return True
    row = _chart_row(chart_id)
    if not row:
        return False
    output_path = (row.get("output_path") or "").strip()
    if not output_path:
        return False
    for candidate in _local_output_ocr_candidates(output_path, chart_name):
        if ocr_artifacts_in_dir(candidate, chart_name):
            return True
    container = (row.get("blob_container") or "").strip()
    if not container:
        return False
    try:
        from db.blob_store import list_blobs_with_suffixes

        prefix = output_path.strip().strip("/").replace("\\", "/")
        for pref in (f"{prefix}/ocr", prefix):
            names = list_blobs_with_suffixes(container, pref, (".txt", ".json"))
            wanted = set(_ocr_filenames(chart_name))
            if any(Path(n).name in wanted for n in names):
                return True
    except Exception:
        logger.debug(
            "SKIP_OCR: blob probe failed for %s/%s", container, output_path,
            exc_info=True,
        )
    return False


def ocr_results_present(chart_id: int) -> bool:
    """True when ``ocr_results`` has at least one non-empty row for this chart."""
    with connect() as conn:
        row = conn.execute(
            """
            SELECT 1 AS ok
              FROM ocr_results
             WHERE chart_id = %s
               AND COALESCE(char_count, 0) > 0
             LIMIT 1
            """,
            (chart_id,),
        ).fetchone()
    return bool(row)


def _page_doc_from_raw(
    raw: str,
    *,
    page_name: str,
    page_number: Any,
    ocr_type: str,
) -> dict[str, Any]:
    """Rebuild a final1/final2 page object from a stored ``raw_text`` cell."""
    base: dict[str, Any] = {
        "pageNumber": page_number,
        "fileName": page_name,
        "content": "",
    }
    if not raw:
        return base
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        base["content"] = raw
        if ocr_type == "docling":
            base["markdown"] = raw
        return base
    if isinstance(parsed, dict):
        base.update(parsed)
        base["fileName"] = page_name
        base["pageNumber"] = page_number
        if "content" not in base and "markdown" in base:
            base["content"] = base.get("markdown") or ""
        return base
    base["content"] = str(parsed)
    if ocr_type == "docling":
        base["markdown"] = base["content"]
    return base


def materialize_ocr_from_db(chart_id: int, chart_name: str) -> dict[str, Any]:
    """Write the three ``ocr/`` files from ``ocr_results`` and mark stages done."""
    summary: dict[str, Any] = {
        "chart_id": chart_id,
        "chart_name": chart_name,
        "source": "ocr_results",
        "written": {},
        "stages_marked": [],
    }
    with connect() as conn:
        pages = list_pages(conn, chart_id)
        page_ids = [p["id"] for p in pages]
        loaded_kinds: list[str] = []

        prelim = get_ocr_texts(conn, chart_id, "tesseract")
        if prelim:
            page_texts = [
                (p["page_name"], prelim.get(p["id"], ""))
                for p in pages
                if p["id"] in prelim
            ]
            if page_texts:
                path = write_combined_ocr_txt(chart_name, "prelim", page_texts)
                summary["written"]["prelim"] = str(path)
                loaded_kinds.append("ocr_prelim")

        final1 = get_ocr_texts(conn, chart_id, "docling")
        if final1:
            out_pages = [
                _page_doc_from_raw(
                    final1.get(p["id"], ""),
                    page_name=p["page_name"],
                    page_number=p.get("page_number"),
                    ocr_type="docling",
                )
                for p in pages
                if p["id"] in final1
            ]
            if out_pages:
                path = write_final1_json(chart_name, out_pages, model="skip_ocr_from_db")
                summary["written"]["final1"] = str(path)
                loaded_kinds.append("ocr_final1")

        final2 = get_ocr_texts(conn, chart_id, "azuredocintel")
        if final2:
            out_pages = [
                _page_doc_from_raw(
                    final2.get(p["id"], ""),
                    page_name=p["page_name"],
                    page_number=p.get("page_number"),
                    ocr_type="azuredocintel",
                )
                for p in pages
                if p["id"] in final2
            ]
            if out_pages:
                path = write_final2_json(chart_name, out_pages)
                summary["written"]["final2"] = str(path)
                loaded_kinds.append("ocr_final2")

        for stage_name in OCR_STAGE_NAMES:
            if not page_ids:
                continue
            set_pages_stage(
                conn,
                chart_id=chart_id,
                page_ids=page_ids,
                stage_name=stage_name,
                pass_no=1,
                status="completed" if stage_name in loaded_kinds else "skipped",
                skip_reason=(
                    None
                    if stage_name in loaded_kinds
                    else "skip_ocr_no_artifact"
                ),
            )
            summary["stages_marked"].append(stage_name)

    logger.info(
        "SKIP_OCR: materialized chart %s ocr/ from DB — %s",
        chart_name,
        list(summary["written"]),
    )
    return summary


def hydrate_ocr_from_disk(
    chart_id: int,
    chart_name: str,
    *,
    root: Optional[Path] = None,
) -> dict[str, Any]:
    """Load existing ocr/ files into ``ocr_results`` and mark OCR stages done."""
    ocr_root = root or ocr_dir(chart_name)
    summary: dict[str, Any] = {
        "chart_id": chart_id,
        "chart_name": chart_name,
        "source": f"disk:{ocr_root}",
        "loaded": {},
        "stages_marked": [],
    }
    with connect() as conn:
        pages = list_pages(conn, chart_id)
        by_name = {p["page_name"]: p["id"] for p in pages}
        page_ids = [p["id"] for p in pages]

        loaded_kinds: list[str] = []

        prelim_path = ocr_root / f"{chart_name}_prelim.txt"
        if prelim_path.is_file() and prelim_path.stat().st_size > 0:
            texts = parse_combined_ocr_txt(prelim_path)
            count = 0
            for name, text in texts.items():
                page_id = by_name.get(name)
                if page_id is None:
                    continue
                upsert_ocr_result(
                    conn,
                    chart_id=chart_id,
                    page_id=page_id,
                    ocr_type="tesseract",
                    raw_text=text,
                )
                count += 1
            summary["loaded"]["tesseract"] = count
            loaded_kinds.append("ocr_prelim")

        for kind, ocr_type, filename in (
            ("ocr_final1", "docling", f"{chart_name}_final1.json"),
            ("ocr_final2", "azuredocintel", f"{chart_name}_final2.json"),
        ):
            path = ocr_root / filename
            if not path.is_file() or path.stat().st_size == 0:
                continue
            try:
                # Prefer full JSON pages (keeps section_headers / polygons).
                doc = json.loads(path.read_text(encoding="utf-8"))
                pages_in = doc.get("pages") if isinstance(doc, dict) else None
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                logger.warning("SKIP_OCR: could not parse %s (%s)", path.name, exc)
                continue

            count = 0
            if isinstance(pages_in, list) and pages_in:
                for page_obj in pages_in:
                    if not isinstance(page_obj, dict):
                        continue
                    name = str(
                        page_obj.get("fileName")
                        or page_obj.get("filename")
                        or ""
                    )
                    page_id = by_name.get(name)
                    if page_id is None:
                        continue
                    upsert_ocr_result(
                        conn,
                        chart_id=chart_id,
                        page_id=page_id,
                        ocr_type=ocr_type,
                        raw_text=json.dumps(page_obj, default=str),
                    )
                    count += 1
            else:
                try:
                    texts = parse_ocr_json(path)
                except (ValueError, TypeError, json.JSONDecodeError) as exc:
                    logger.warning("SKIP_OCR: could not parse %s (%s)", path.name, exc)
                    continue
                for name, text in texts.items():
                    page_id = by_name.get(name)
                    if page_id is None:
                        continue
                    page = next((p for p in pages if p["page_name"] == name), None)
                    raw = json.dumps(
                        {
                            "pageNumber": page.get("page_number") if page else None,
                            "fileName": name,
                            "content": text,
                            "markdown": text,
                            "engine": "skip_ocr_reuse",
                        }
                    )
                    upsert_ocr_result(
                        conn,
                        chart_id=chart_id,
                        page_id=page_id,
                        ocr_type=ocr_type,
                        raw_text=raw,
                    )
                    count += 1
            summary["loaded"][ocr_type] = count
            if count:
                loaded_kinds.append(kind)

        for stage_name in OCR_STAGE_NAMES:
            if not page_ids:
                continue
            set_pages_stage(
                conn,
                chart_id=chart_id,
                page_ids=page_ids,
                stage_name=stage_name,
                pass_no=1,
                status="completed" if stage_name in loaded_kinds else "skipped",
                skip_reason=(
                    None
                    if stage_name in loaded_kinds
                    else "skip_ocr_no_artifact"
                ),
            )
            summary["stages_marked"].append(stage_name)

    logger.info(
        "SKIP_OCR: hydrated chart %s from disk — %s",
        chart_name,
        summary["loaded"],
    )
    return summary


def apply_skip_ocr(chart_id: int, chart_name: str) -> dict[str, Any]:
    """Output folder → workspace disk → DB materialize. Skip already decided."""
    synced = sync_ocr_from_output_folder(chart_id, chart_name)
    if ocr_artifacts_present(chart_name):
        result = hydrate_ocr_from_disk(chart_id, chart_name)
        if synced:
            result["output_sync"] = synced
        return result
    return materialize_ocr_from_db(chart_id, chart_name)


def should_skip_ocr_stages(
    *,
    chart_name: str,
    chart_id: Optional[int] = None,
    force: bool,
    skip_ocr: Optional[bool] = None,
) -> bool:
    """Whether the orchestrator should bypass OCR engines for this chart.

    ``skip_ocr`` is the per-request override (API/CLI). ``None`` falls back to
    the ``SKIP_OCR`` env flag. ``force=True`` always runs OCR.
    """
    from config import SKIP_OCR

    enabled = SKIP_OCR if skip_ocr is None else bool(skip_ocr)
    if force or not enabled:
        return False
    if ocr_artifacts_present(chart_name):
        return True
    if chart_id is not None and output_or_workspace_ocr_ready(chart_id, chart_name):
        return True
    if chart_id is not None and ocr_results_present(chart_id):
        return True
    logger.info(
        "skip_ocr requested but no usable output/workspace ocr/ or ocr_results "
        "for %s — running OCR",
        chart_name,
    )
    return False
