from __future__ import annotations

import csv
import json
import os
import re
import sys
import threading
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from app.adapters.base import FolderRepository
from app.core.schemas import (
    FolderDetail,
    FolderSummary,
    ImagingDocumentResponse,
    ImagingManifestDetails,
    ImagingPageResult,
    ImagingSectionsProcessed,
    OcrKind,
    OcrRunStatus,
    OcrSectionHeader,
    OcrTextResponse,
    PageSummary,
)
from app.services.metadata_csv import manifest_for_record, run_batch_for_record

PAGE_RE = re.compile(r"^page_(\d+)\.(jpe?g|png|webp|tif{1,2})$", re.IGNORECASE)
PLAIN_NUM_RE = re.compile(r"^(\d+)\.(jpe?g|png|webp|tif{1,2})$", re.IGNORECASE)
IMAGE_RE = re.compile(r"\.(jpe?g|png|webp|tif{1,2})$", re.IGNORECASE)

# API kind → filename suffix: <folder>_<suffix>.{txt|json}
KIND_TO_SUFFIX: dict[str, str] = {
    "preliminary": "prelim",
    "final1": "final1",
    "final2": "final2",
}

OCR_KINDS: tuple[str, ...] = ("preliminary", "final1", "final2")
# final1 (Docling/OSS) and final2 (AzDocInt) are JSON; prelim is plain text.
# final1 still accepts .txt so older workspaces keep working.
KIND_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "preliminary": (".txt",),
    "final1": (".json", ".txt"),
    "final2": (".json", ".txt"),
}


def _page_num_from_name(name: str) -> int | None:
    match = PAGE_RE.match(name) or PLAIN_NUM_RE.match(name)
    if not match:
        return None
    return int(match.group(1))


def _fmt_dos_display(raw: str | date | None) -> str | None:
    """Normalize dates to MM/DD/YYYY for the Imaging UI."""
    if raw is None:
        return None
    if isinstance(raw, date):
        return raw.strftime("%m/%d/%Y")
    value = str(raw).strip()
    if not value or value.lower() in {"unknown", "null", "none"}:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%m/%d/%y", "%m-%d-%y"):
        try:
            return datetime.strptime(value, fmt).strftime("%m/%d/%Y")
        except ValueError:
            continue
    return value


def _dos_row_fields(row: dict[str, str]) -> dict[str, str | None]:
    dos_from = _fmt_dos_display(row.get("dos_from_iso") or row.get("dos_from") or row.get("dos"))
    dos_to = _fmt_dos_display(row.get("dos_to_iso") or row.get("dos_to") or "")
    if dos_from and not dos_to and (row.get("dos_from") or row.get("dos_from_iso") or row.get("dos")):
        dos_to = dos_from
    return {
        "dosFrom": dos_from,
        "dosTo": dos_to,
        "docDosFrom": _fmt_dos_display(
            row.get("doc_dos_from_iso") or row.get("doc_dos_from") or ""
        ),
        "docDosTo": _fmt_dos_display(row.get("doc_dos_to_iso") or row.get("doc_dos_to") or ""),
    }


def _read_dos_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.is_file() or path.stat().st_size == 0:
        return []
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows: list[dict[str, str]] = []
        for raw in reader:
            row = {(k or "").strip(): (v or "").strip() for k, v in raw.items() if k is not None}
            if row:
                rows.append(row)
        return rows


def _index_dos_rows(rows: list[dict[str, str]], chart_name: str) -> dict[str, dict[str, str | None]]:
    """Map page filename / page number → DOS fields for one chart."""
    by_key: dict[str, dict[str, str | None]] = {}
    for row in rows:
        cname = (row.get("chart_name") or chart_name).strip()
        if cname and cname != chart_name:
            continue
        fields = _dos_row_fields(row)
        page_name = (row.get("page_name") or "").strip()
        if page_name:
            by_key[page_name.lower()] = fields
            by_key[Path(page_name).name.lower()] = fields
        raw_num = (row.get("page_number") or "").strip()
        if raw_num.isdigit():
            by_key[f"#{raw_num}"] = fields
    return by_key


def _overlay_dos_on_pages(
    pages: list[ImagingPageResult],
    dos_by_key: dict[str, dict[str, str | None]],
) -> list[ImagingPageResult]:
    if not dos_by_key:
        return pages
    out: list[ImagingPageResult] = []
    for page in pages:
        hit = (
            dos_by_key.get(page.fileName.lower())
            or dos_by_key.get(Path(page.fileName).name.lower())
            or dos_by_key.get(f"#{page.pageNumber}")
        )
        if not hit:
            out.append(page)
            continue
        out.append(
            page.model_copy(
                update={
                    "dosFrom": hit.get("dosFrom"),
                    "dosTo": hit.get("dosTo"),
                    "docDosFrom": hit.get("docDosFrom"),
                    "docDosTo": hit.get("docDosTo"),
                }
            )
        )
    return out


def _index_hw_rows(
    rows: list[dict[str, str]], chart_name: str
) -> dict[str, dict[str, Any]]:
    from app.services.imaging_overlays import index_hw_rows

    return index_hw_rows(rows, chart_name)


def _overlay_hw_on_pages(
    pages: list[ImagingPageResult],
    hw_by_key: dict[str, dict[str, Any]],
) -> list[ImagingPageResult]:
    if not hw_by_key:
        return pages
    out: list[ImagingPageResult] = []
    for page in pages:
        hit = (
            hw_by_key.get(page.fileName.lower())
            or hw_by_key.get(Path(page.fileName).name.lower())
            or hw_by_key.get(f"#{page.pageNumber}")
        )
        if not hit:
            out.append(page)
            continue
        out.append(page.model_copy(update=hit))
    return out


def _mtime(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def _latest_mtime(paths: list[Path]) -> datetime | None:
    times = [t for p in paths if (t := _mtime(p)) is not None]
    return max(times) if times else None


def _normalize_kind(kind: str) -> OcrKind:
    if kind not in KIND_TO_SUFFIX:
        raise HTTPException(
            status_code=400,
            detail="kind must be preliminary, final1, or final2",
        )
    return kind  # type: ignore[return-value]


# Shown in review-ui when Final OCR was skipped.
FINAL2_QUALITY_SKIP_MESSAGE = "Skipped for High Quality Images"
FINAL_OCR_BLANK_JUNK_SKIP_MESSAGE = "Skipped for Blank/Junk"


def clean_ocr_display_text(text: str) -> str:
    """Remove Docling image placeholders, empty tables, and excess blank lines."""
    if not text:
        return ""
    out = re.sub(r"<!--\s*image\s*-->", "", text, flags=re.IGNORECASE)
    out = re.sub(r"(?im)^\s*\[image\]\s*$", "", out)
    cleaned_lines: list[str] = []
    for line in out.splitlines():
        stripped = line.strip()
        if not stripped:
            cleaned_lines.append("")
            continue
        if re.fullmatch(r"\|?[\s\-:|]+\|?", stripped):
            continue
        if stripped.startswith("|"):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if all(c == "" for c in cells):
                continue
        cleaned_lines.append(line.rstrip())
    collapsed: list[str] = []
    blank = False
    for line in cleaned_lines:
        if not line.strip():
            if blank:
                continue
            collapsed.append("")
            blank = True
        else:
            collapsed.append(line)
            blank = False
    return "\n".join(collapsed).strip()


def _image_size_for_page_file(folder_dir: Path, filename: str) -> tuple[float, float] | None:
    """Native pixel size of the page image the UI shows (corrected if present)."""
    try:
        from PIL import Image
    except ImportError:
        return None
    stem = Path(filename).stem
    corrected = folder_dir / "corrected-pages"
    candidates = [
        corrected / f"{stem}.jpg",
        corrected / filename,
        folder_dir / "pages" / filename,
        folder_dir / "pages" / f"{stem}.jpg",
        folder_dir / filename,
    ]
    for path in candidates:
        if path.is_file() and path.stat().st_size > 0:
            try:
                with Image.open(path) as im:
                    w, h = im.size
                    if w > 0 and h > 0:
                        return float(w), float(h)
            except Exception:
                continue
    return None


def _page_dims_for_overlay(
    page_w: float,
    page_h: float,
    image_size: tuple[float, float] | None,
    *,
    unit: str | None = None,
    bboxes: list[tuple[float, float, float, float]] | None = None,
) -> tuple[float, float]:
    """Return ``(page_w, page_h)`` used with OCR coords (same unit as bbox).

    When ``image_size`` is known, callers should map OCR→image via
    ``scale = image / page`` (document-processing pattern) rather than replacing
    page_* with pixel dims — that broke Azure inch pages and consistent 2×
    Docling page spaces.
    """
    del unit, bboxes  # kept for call-site compatibility
    if page_w <= 0 or page_h <= 0:
        if image_size:
            return image_size
        return 1.0, 1.0
    return page_w, page_h


def _ocr_box_to_image_fractions(
    l: float,
    t: float,
    r: float,
    b: float,
    page_w: float,
    page_h: float,
    image_size: tuple[float, float] | None,
    *,
    origin: str = "TOPLEFT",
) -> tuple[float, float, float, float]:
    """OCR-space box → CSS fractions of the *displayed* page image.

    Same mapping as advantmed-document-processing headings features::

        scale_x = image_w / page_w
        scale_y = image_h / page_h

    Then divide by image size. When page_* and the polygon share a unit
    (pixels or inches), fractions land on the file the UI shows — no re-OCR
    required for overlay alignment.
    """
    pw = page_w if page_w > 0 else 0.0
    ph = page_h if page_h > 0 else 0.0
    if image_size and pw > 0 and ph > 0:
        iw, ih = image_size
        if iw > 0 and ih > 0:
            sx = iw / pw
            sy = ih / ph
            l, t, r, b = l * sx, t * sy, r * sx, b * sy
            pw, ph = iw, ih

    origin_u = (origin or "TOPLEFT").upper().replace("-", "").replace("_", "")
    if t < b and origin_u.startswith("BOTTOM"):
        origin_u = "TOPLEFT"
    elif t > b and origin_u.startswith("TOP"):
        origin_u = "BOTTOMLEFT"

    if pw <= 0 or ph <= 0:
        return 0.0, 0.0, 0.0, 0.0

    left = min(l, r) / pw
    width = abs(r - l) / pw
    if origin_u.startswith("BOTTOM"):
        top_y = max(t, b)
        bot_y = min(t, b)
        top = (ph - top_y) / ph
        height = (top_y - bot_y) / ph
    else:
        top = min(t, b) / ph
        height = abs(b - t) / ph

    def clip(v: float) -> float:
        return max(0.0, min(1.0, float(v)))

    return clip(left), clip(top), clip(width), clip(height)


def _section_headers_from_page(
    page: dict[str, Any],
    *,
    image_size: tuple[float, float] | None = None,
) -> list[OcrSectionHeader]:
    """Pull CSS-normalized header boxes from a final1/final2 page object.

    Headers without coordinates are still returned (left/top/width/height = 0)
    so the UI can style them in the OCR text; only boxes with positive size
    are drawn on the image.

    Accepts ``section_headers`` with ``norm``, ``bbox``, or Azure ``polygon``.
    When headers are missing, derives candidates from ``pagesMeta[].lines``.
    ``image_size`` corrects half-scale page_* vs the displayed file (pixel
    units only — inch/cm from Azure DI are left alone).
    """
    raw = page.get("section_headers") or page.get("sectionHeaders") or []
    if not isinstance(raw, list) or not raw:
        raw = _headers_from_azure_pages_meta(page)
    if not isinstance(raw, list):
        return []

    out: list[OcrSectionHeader] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or item.get("content") or "").strip()
        if not text:
            continue
        left = top = width = height = 0.0
        # Prefer recomputing from raw bbox/polygon. Map OCR space → image
        # fractions via scale = image/page (document-processing pattern).
        if isinstance(item.get("bbox"), (list, tuple)) and len(item["bbox"]) >= 4:
            try:
                l, t, r, b = (float(x) for x in item["bbox"][:4])
                pw = float(item.get("page_width") or 0) or 0.0
                ph = float(item.get("page_height") or 0) or 0.0
                origin = str(item.get("coord_origin") or "TOPLEFT")
                left, top, width, height = _ocr_box_to_image_fractions(
                    l, t, r, b, pw, ph, image_size, origin=origin
                )
            except (TypeError, ValueError):
                left = top = width = height = 0.0
        elif isinstance(item.get("polygon"), (list, tuple)) and len(item["polygon"]) >= 8:
            try:
                poly = [float(x) for x in item["polygon"]]
                xs, ys = poly[0::2], poly[1::2]
                l, t, r, b = min(xs), min(ys), max(xs), max(ys)
                pw = float(
                    item.get("page_width")
                    or item.get("pageWidth")
                    or 0
                ) or 0.0
                ph = float(
                    item.get("page_height")
                    or item.get("pageHeight")
                    or 0
                ) or 0.0
                left, top, width, height = _ocr_box_to_image_fractions(
                    l, t, r, b, pw, ph, image_size, origin="TOPLEFT"
                )
            except (TypeError, ValueError):
                left = top = width = height = 0.0
        else:
            norm = item.get("norm") if isinstance(item.get("norm"), dict) else None
            if norm:
                try:
                    left = float(norm.get("left") or 0)
                    top = float(norm.get("top") or 0)
                    width = float(norm.get("width") or 0)
                    height = float(norm.get("height") or 0)
                    # If we have image + page_* and a bbox was missing, stored
                    # norm is already a fraction of page_* — remap when page
                    # aspect was used as if it were the image (rare). Prefer
                    # bbox/polygon paths above whenever present.
                except (TypeError, ValueError):
                    left = top = width = height = 0.0
        try:
            level = int(item.get("level") or 2)
        except (TypeError, ValueError):
            level = 2
        out.append(
            OcrSectionHeader(
                text=text,
                level=level,
                left=left,
                top=top,
                width=width,
                height=height,
            )
        )
    return out


def _headers_from_azure_pages_meta(page: dict[str, Any]) -> list[dict[str, Any]]:
    """Build header candidates from Azure ``pagesMeta[].lines`` (with polygons).

    Every non-empty line is a candidate; live canon filtering in
    ``_filter_headers_against_canon`` / ``filter_section_headers`` decides
    what stays — no regex / ALL-CAPS shortlist.
    """
    metas = page.get("pagesMeta") or page.get("pages_meta") or []
    if not isinstance(metas, list):
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for meta in metas:
        if not isinstance(meta, dict):
            continue
        try:
            pw = float(meta.get("width") or 0) or 0.0
            ph = float(meta.get("height") or 0) or 0.0
        except (TypeError, ValueError):
            pw = ph = 0.0
        for line in meta.get("lines") or []:
            if not isinstance(line, dict):
                continue
            text = str(line.get("content") or "").strip()
            if not text:
                continue
            key = " ".join(text.split()).casefold()
            if key in seen:
                continue
            seen.add(key)
            poly = line.get("polygon") or []
            item: dict[str, Any] = {
                "text": text,
                "level": 2,
                "polygon": poly,
                "page_width": pw or None,
                "page_height": ph or None,
                "unit": meta.get("unit"),
                "coord_origin": "TOPLEFT",
            }
            if isinstance(poly, (list, tuple)) and len(poly) >= 8 and pw and ph:
                try:
                    vals = [float(x) for x in poly]
                    xs, ys = vals[0::2], vals[1::2]
                    l, t, r, b = min(xs), min(ys), max(xs), max(ys)
                    item["bbox"] = [l, t, r, b]
                    item["norm"] = {
                        "left": max(0.0, min(1.0, min(l, r) / pw)),
                        "top": max(0.0, min(1.0, min(t, b) / ph)),
                        "width": max(0.0, min(1.0, abs(r - l) / pw)),
                        "height": max(0.0, min(1.0, abs(b - t) / ph)),
                    }
                except (TypeError, ValueError):
                    pass
            out.append(item)
    return out


def _core_pipeline_dir() -> Path | None:
    """Monorepo ``core-pipeline/`` when present (for live canon filtering)."""
    try:
        from app.core.config import get_settings

        root = get_settings().resolved_monorepo_root
        core = Path(root) / "core-pipeline"
        if core.is_dir():
            return core
    except Exception:
        pass
    # review-ui/backend/app/adapters/local → parents[4] = monorepo
    here = Path(__file__).resolve()
    for parent in here.parents:
        core = parent / "core-pipeline"
        if core.is_dir() and (core / "stages" / "lib" / "imaging").is_dir():
            return core
    return None


def _filter_headers_against_canon(
    headers: list[OcrSectionHeader],
) -> list[OcrSectionHeader]:
    """Keep headers with ≥90% lexical/semantic match to ``section_header_canon.json``.

    Re-runs on every OCR fetch so editing the list updates overlays without
    re-OCR. Falls back to exact normalized match if the matcher cannot import.
    """
    if not headers:
        return headers
    core = _core_pipeline_dir()
    if core is not None:
        core_s = str(core)
        if core_s not in sys.path:
            sys.path.insert(0, core_s)
        try:
            from stages.lib.imaging.section_header_match import (  # type: ignore
                filter_section_headers,
            )

            as_dicts = [
                {
                    "text": h.text,
                    "level": h.level,
                    "left": h.left,
                    "top": h.top,
                    "width": h.width,
                    "height": h.height,
                }
                for h in headers
            ]
            kept = filter_section_headers(
                as_dicts, threshold=0.90, enabled=True, use_minilm=False
            )
            out: list[OcrSectionHeader] = []
            for item in kept:
                # Prefer the canon label so OCR noise like "4 Allergies" → "Allergies".
                label = str(
                    item.get("matched_canonical") or item.get("text") or ""
                ).strip()
                out.append(
                    OcrSectionHeader(
                        text=label,
                        level=int(item.get("level") or 2),
                        left=float(item.get("left") or 0),
                        top=float(item.get("top") or 0),
                        width=float(item.get("width") or 0),
                        height=float(item.get("height") or 0),
                    )
                )
            return out
        except Exception as exc:
            logger = __import__("logging").getLogger(__name__)
            logger.warning("Live section-header canon filter skipped: %s", exc)

    # Exact-match fallback against the JSON list (no MiniLM / difflib).
    canon_path = None
    if core is not None:
        canon_path = (
            core / "stages" / "lib" / "imaging" / "section_header_canon.json"
        )
    if canon_path is None or not canon_path.is_file():
        return headers
    try:
        raw = json.loads(canon_path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            return headers

        def _norm(s: str) -> str:
            t = (s or "").strip().lower()
            t = re.sub(r"[:*\-–—|/]+$", "", t)
            t = re.sub(r"[^\w\s/+]+", " ", t)
            return re.sub(r"\s+", " ", t).strip()

        allowed = {_norm(str(x)) for x in raw if str(x).strip()}
        return [h for h in headers if _norm(h.text) in allowed]
    except Exception:
        return headers


def section_headers_by_file_from_ocr_json(
    data: Any,
    *,
    folder_dir: Path | None = None,
) -> dict[str, list[OcrSectionHeader]]:
    """Map fileName → header boxes from a combined final1/final2 JSON doc.

    Headers are re-filtered against the live canon list (≥90% match) so list
    edits apply immediately without re-running Final1. When ``folder_dir`` is
    set, boxes are rescaled against the displayed page image (fixes half-size
    overlays from Docling ``images_scale=2``).
    """
    if isinstance(data, list):
        pages = data
    elif isinstance(data, dict):
        pages = data.get("pages") or []
    else:
        return {}
    if not isinstance(pages, list):
        return {}
    by_file: dict[str, list[OcrSectionHeader]] = {}
    for idx, page in enumerate(pages, start=1):
        if not isinstance(page, dict):
            continue
        filename = str(
            page.get("fileName")
            or page.get("filename")
            or page.get("file_name")
            or f"{page.get('pageNumber') or idx}.jpg"
        )
        image_size = (
            _image_size_for_page_file(folder_dir, filename)
            if folder_dir is not None
            else None
        )
        headers = _filter_headers_against_canon(
            _section_headers_from_page(page, image_size=image_size)
        )
        if headers:
            by_file[filename] = headers
            by_file[filename.lower()] = headers
    return by_file


def _page_content_from_azdoc(page: dict[str, Any]) -> str:
    content = page.get("content")
    if isinstance(content, str) and content.strip():
        return clean_ocr_display_text(content)
    markdown = page.get("markdown")
    if isinstance(markdown, str) and markdown.strip():
        return clean_ocr_display_text(markdown)
    lines = page.get("lines")
    if isinstance(lines, list):
        parts = [
            str(line.get("content", "")).strip()
            for line in lines
            if isinstance(line, dict) and line.get("content")
        ]
        if parts:
            return clean_ocr_display_text("\n".join(parts))
    reason = str(
        page.get("skippedReason") or page.get("skipped_reason") or ""
    ).strip().lower()
    if reason == "high_quality_printed":
        return FINAL2_QUALITY_SKIP_MESSAGE
    if reason in {"blank_junk_pass1", "blank_junk", "blank_junk_pass2"}:
        return FINAL_OCR_BLANK_JUNK_SKIP_MESSAGE
    return ""


def azdoc_json_to_ocr_text(data: Any) -> str:
    """Convert AzDocInt / Final1 JSON into marker-separated OCR text for the UI.

    Expected shape (per document):
      { "pages": [ { "fileName": "1.jpg", "content": "...", "lines": [...] }, ... ] }
    """
    if isinstance(data, list):
        pages = data
    elif isinstance(data, dict):
        pages = data.get("pages") or []
    else:
        return ""

    if not isinstance(pages, list):
        return ""

    chunks: list[str] = []
    for idx, page in enumerate(pages, start=1):
        if not isinstance(page, dict):
            continue
        filename = (
            page.get("fileName")
            or page.get("filename")
            or page.get("file_name")
            or f"{page.get('pageNumber') or idx}.jpg"
        )
        body = _page_content_from_azdoc(page)
        chunks.append(f"===== {filename} =====\n{body}".rstrip())
    if not chunks:
        return ""
    return "\n\n".join(chunks) + "\n"


class LocalFolderRepository(FolderRepository):
    """Reads document folders from a local filesystem tree.

    Layout per folder:
      pages/1.jpg …
      ocr/<folder_name>_prelim.txt    # Preliminary (Tess)
      ocr/<folder_name>_final1.json   # Final (OSS / Docling) — pages[].content
      ocr/<folder_name>_final2.json   # Final (AzDocInt) — pages[].content
      # text OCR sections (API response): ===== 1.jpg =====
      # Legacy: ocr/<folder_name>_final1.txt is still accepted.

    Manifest Details (DATA_MODE=local): metadata_R{n}_B{n}.csv under data/pipeline
    (falls back to 06-postgres-db/manifest). Postgres mode reads manifest_member_list.
    """

    # How long a cached scan of data/folders is trusted before it is rechecked.
    # The pipeline writes into this directory while the UI is serving, so a
    # cache with no expiry showed a chart's badges frozen at whatever was on
    # disk when the process started.
    CACHE_TTL_SECONDS = float(os.environ.get("LOCAL_CACHE_TTL_SECONDS") or "5")

    def __init__(self, data_root: Path, metadata_root: Path | None = None):
        self.data_root = data_root
        self.metadata_root = metadata_root
        self._overlay_chart_ids: set[str] | None = None
        # chart → which of the pipeline outputs are present (excl. manifest)
        self._pipeline_streams: dict[str, set[str]] | None = None
        # chart → page numbers seen in any page-level pipeline CSV
        self._pipeline_pages: dict[str, set[int]] | None = None
        self._cache_stamp: tuple[float, tuple] | None = None
        self._cache_lock = threading.Lock()

    def _scan_signature(self) -> tuple:
        """Cheap fingerprint of the imaging outputs on disk.

        Directory mtimes plus each imaging CSV's (size, mtime). A stage that
        rewrites a CSV changes it, so the next request rebuilds the scan.
        """
        parts: list[tuple] = []
        if not self.data_root.is_dir():
            return ()
        try:
            for entry in sorted(self.data_root.iterdir()):
                if not entry.is_dir() or entry.name.startswith("."):
                    continue
                imaging = entry / "imaging"
                if not imaging.is_dir():
                    parts.append((entry.name, None))
                    continue
                for path in sorted(imaging.iterdir()):
                    if not path.is_file() or path.name.startswith("._"):
                        continue
                    stat = path.stat()
                    parts.append((entry.name, path.name, stat.st_size, stat.st_mtime))
        except OSError:
            return ()
        return tuple(parts)

    def _cache_valid(self) -> bool:
        if self._pipeline_streams is None or self._cache_stamp is None:
            return False
        checked_at, signature = self._cache_stamp
        if (time.monotonic() - checked_at) < self.CACHE_TTL_SECONDS:
            return True
        if signature == self._scan_signature():
            # Unchanged on disk — keep the scan, restart the TTL.
            self._cache_stamp = (time.monotonic(), signature)
            return True
        return False

    def invalidate_cache(self) -> None:
        with self._cache_lock:
            self._pipeline_streams = None
            self._pipeline_pages = None
            self._overlay_chart_ids = None
            self._cache_stamp = None

    def _folder_dir(self, folder_id: str) -> Path:
        if "/" in folder_id or "\\" in folder_id or folder_id in (".", ".."):
            raise HTTPException(status_code=400, detail="Invalid folder id")
        path = (self.data_root / folder_id).resolve()
        if not str(path).startswith(str(self.data_root.resolve())):
            raise HTTPException(status_code=400, detail="Invalid folder id")
        if not path.is_dir():
            raise HTTPException(status_code=404, detail=f"Folder not found: {folder_id}")
        return path

    def _ocr_path(self, folder_dir: Path, kind: str) -> Path:
        suffix = KIND_TO_SUFFIX.get(kind)
        if not suffix:
            raise HTTPException(
                status_code=400,
                detail="kind must be preliminary, final1, or final2",
            )
        ocr_dir = folder_dir / "ocr"
        for ext in KIND_EXTENSIONS.get(kind, (".txt",)):
            path = ocr_dir / f"{folder_dir.name}_{suffix}{ext}"
            if path.is_file():
                return path
        preferred_ext = KIND_EXTENSIONS.get(kind, (".txt",))[0]
        return ocr_dir / f"{folder_dir.name}_{suffix}{preferred_ext}"

    def _has_ocr(self, folder_dir: Path, kind: str) -> bool:
        path = self._ocr_path(folder_dir, kind)
        try:
            return path.is_file() and path.stat().st_size > 0
        except OSError:
            return False

    def _imaging_path(self, folder_dir: Path) -> Path:
        return folder_dir / "imaging" / f"{folder_dir.name}_imaging.json"

    def _overlay_chart_id_set(self) -> set[str]:
        """Chart ids that appear in any combined pipeline CSV (cached once)."""
        if self._overlay_chart_ids is not None:
            return self._overlay_chart_ids

        from app.services.imaging_overlays import (
            chart_id_key,
            read_csv_rows,
            resolve_pipeline_csv,
        )

        ids: set[str] = set()
        combined_files = [
            resolve_pipeline_csv(
                self.data_root,
                "02-imaging-pipeline",
                "dos-extraction",
                "output",
                "dos_extraction.csv",
            ),
            resolve_pipeline_csv(
                self.data_root, "01-ocr-extraction", "output", "hw_printed.csv"
            ),
            resolve_pipeline_csv(
                self.data_root,
                "02-imaging-pipeline",
                "rotation-orientation",
                "output",
                "rotation.csv",
            ),
            resolve_pipeline_csv(
                self.data_root,
                "02-imaging-pipeline",
                "member-verification",
                "output",
                "member_extraction_results.csv",
            ),
            resolve_pipeline_csv(
                self.data_root,
                "02-imaging-pipeline",
                "member-verification",
                "output",
                "member_verification_summary.csv",
            ),
            resolve_pipeline_csv(
                self.data_root,
                "02-imaging-pipeline",
                "junk-classification",
                "output",
                "junk_classification.csv",
            ),
        ]
        for path in combined_files:
            for row in read_csv_rows(path):
                for col in ("chart_name", "folder", "chart_id"):
                    val = (row.get(col) or "").strip()
                    if val:
                        ids.add(val)
                        ids.add(chart_id_key(val))
        self._overlay_chart_ids = ids
        return ids

    def _has_imaging(self, folder_dir: Path) -> bool:
        """Cheap check for History listing — no per-folder full CSV scans."""
        path = self._imaging_path(folder_dir)
        try:
            if path.is_file() and path.stat().st_size > 0:
                return True
        except OSError:
            pass
        imaging_dir = folder_dir / "imaging"
        if imaging_dir.is_dir():
            for entry in imaging_dir.iterdir():
                if (
                    entry.is_file()
                    and not entry.name.startswith("._")
                    and entry.suffix.lower() in {".csv", ".json"}
                    and entry.stat().st_size > 0
                ):
                    return True
        chart = folder_dir.name
        ids = self._overlay_chart_id_set()
        if not ids:
            return False
        from app.services.imaging_overlays import chart_id_key

        return chart in ids or chart_id_key(chart) in ids

    def _imaging_processed_count(self, folder_dir: Path, page_count: int) -> int:
        """Count Imaging pages; Completed charts report page_count."""
        if page_count == 0:
            return 0
        if self._imaging_is_full(folder_dir.name):
            return page_count
        pages = self._page_files(folder_dir)
        ready = self._pipeline_page_set(folder_dir.name)
        if not ready:
            # Chart-level hit (e.g. verification summary only) → show progress as 1+
            if self._pipeline_stream_set(folder_dir.name):
                return min(page_count, max(1, len(self._pipeline_stream_set(folder_dir.name))))
            return 0
        count = 0
        for num, path in pages:
            if self._page_has_pipeline_data(num, path.name, ready):
                count += 1
        return count

    # Streams that mark Imaging Completed (HW optional; counts only for In Progress).
    _IMAGING_COMPLETE_STREAMS = frozenset({"rotation", "dos", "member"})

    def _imaging_is_full(self, chart_name: str) -> bool:
        """True when rotation + DOS + member exist (HW not required)."""
        return self._IMAGING_COMPLETE_STREAMS.issubset(self._pipeline_stream_set(chart_name))

    def _page_has_pipeline_data(
        self, page_number: int, filename: str, ready: set[int]
    ) -> bool:
        if page_number in ready:
            return True
        stem = Path(filename).stem
        if stem.isdigit() and int(stem) in ready:
            return True
        return False

    def _pipeline_page_set(self, chart_name: str) -> set[int]:
        index = self._pipeline_coverage_index()[1]
        from app.services.imaging_overlays import chart_id_key

        return set(index.get(chart_name, set()) | index.get(chart_id_key(chart_name), set()))

    def _pipeline_stream_set(self, chart_name: str) -> set[str]:
        streams, _ = self._pipeline_coverage_index()
        from app.services.imaging_overlays import chart_id_key

        out: set[str] = set()
        out |= streams.get(chart_name, set())
        out |= streams.get(chart_id_key(chart_name), set())
        # Merge keys that match via prefix/full id
        from app.services.imaging_overlays import _chart_row_matches

        for key, vals in streams.items():
            if key in {chart_name, chart_id_key(chart_name)}:
                continue
            if _chart_row_matches(key, chart_name):
                out |= vals
        return out

    def _pipeline_coverage_index(
        self,
    ) -> tuple[dict[str, set[str]], dict[str, set[int]]]:
        """Cached chart → pipeline streams + page numbers.

        Streams (manifest not counted for status):
          hw | rotation | dos | member
        Any CSV row naming the chart counts that stream (extracted values optional).
        Imaging Completed needs rotation + dos + member; hw only affects In Progress.
        """
        if self._cache_valid() and self._pipeline_pages is not None:
            return self._pipeline_streams, self._pipeline_pages  # type: ignore[return-value]

        from app.services.imaging_overlays import (
            _chart_row_matches,
            chart_id_key,
            read_csv_rows,
            resolve_pipeline_csv,
        )

        paths = {
            "hw": resolve_pipeline_csv(
                self.data_root, "01-ocr-extraction", "output", "hw_printed.csv"
            ),
            "rotation": resolve_pipeline_csv(
                self.data_root,
                "02-imaging-pipeline",
                "rotation-orientation",
                "output",
                "rotation.csv",
            ),
            "dos": resolve_pipeline_csv(
                self.data_root,
                "02-imaging-pipeline",
                "dos-extraction",
                "output",
                "dos_extraction.csv",
            ),
            "member_extraction": resolve_pipeline_csv(
                self.data_root,
                "02-imaging-pipeline",
                "member-verification",
                "output",
                "member_extraction_results.csv",
            ),
            "member_verification": resolve_pipeline_csv(
                self.data_root,
                "02-imaging-pipeline",
                "member-verification",
                "output",
                "member_verification_summary.csv",
            ),
        }

        def page_num_from_row(row: dict[str, str]) -> int | None:
            for col in ("page_num", "page_number"):
                raw = (row.get(col) or "").strip()
                if raw.isdigit():
                    return int(raw)
            for col in ("page_name", "filename"):
                raw = (row.get(col) or "").strip()
                if not raw or raw.upper() == "N/A":
                    continue
                stem = Path(raw).stem
                if stem.isdigit():
                    return int(stem)
                m = re.match(r"page[_\s-]?(\d+)", stem, re.IGNORECASE)
                if m:
                    return int(m.group(1))
            return None

        def chart_keys(row: dict[str, str]) -> list[str]:
            raw = (
                row.get("chart_id") or row.get("chart_name") or row.get("folder") or ""
            ).strip()
            if not raw:
                return []
            keys = [raw]
            cid = chart_id_key(raw)
            if cid != raw:
                keys.append(cid)
            return keys

        streams: dict[str, set[str]] = {}
        pages: dict[str, set[int]] = {}

        def mark(stream: str, keys: list[str], page_num: int | None) -> None:
            if not keys:
                return
            for key in keys:
                streams.setdefault(key, set()).add(stream)
                if page_num is not None:
                    pages.setdefault(key, set()).add(page_num)

        # Status: any CSV row naming the chart counts — extracted values optional.
        for row in read_csv_rows(paths["hw"]):
            mark("hw", chart_keys(row), page_num_from_row(row))

        for row in read_csv_rows(paths["rotation"]):
            mark("rotation", chart_keys(row), page_num_from_row(row))

        for row in read_csv_rows(paths["dos"]):
            mark("dos", chart_keys(row), page_num_from_row(row))

        for row in read_csv_rows(paths["member_extraction"]):
            mark("member", chart_keys(row), page_num_from_row(row))

        for row in read_csv_rows(paths["member_verification"]):
            mark("member", chart_keys(row), None)

        # Per-chart overrides under data/folders/*/imaging/
        if self.data_root.is_dir():
            for entry in self.data_root.iterdir():
                if not entry.is_dir() or entry.name.startswith("."):
                    continue
                imaging = entry / "imaging"
                if not imaging.is_dir():
                    continue
                chart = entry.name
                keys = [chart, chart_id_key(chart)]
                for path in imaging.iterdir():
                    if not path.is_file() or path.name.startswith("._"):
                        continue
                    name = path.name.lower()
                    if name.endswith("_hw_printed.csv"):
                        for row in read_csv_rows(path):
                            mark("hw", keys, page_num_from_row(row))
                    elif name.endswith("_quality.csv"):
                        for row in read_csv_rows(path):
                            mark("quality", keys, page_num_from_row(row))
                    elif name.endswith("_rotation.csv"):
                        for row in read_csv_rows(path):
                            mark("rotation", keys, page_num_from_row(row))
                    elif name.endswith("_dos.csv"):
                        for row in read_csv_rows(path):
                            mark("dos", keys, page_num_from_row(row))
                    elif name.endswith("_junk.csv"):
                        # Was missing: the detail view read this file but the
                        # folder list never marked the blank/junk stream, so a
                        # chart that had been classified showed no badge.
                        for row in read_csv_rows(path):
                            mark("junk", keys, page_num_from_row(row))
                    elif name.endswith("_codeable.csv"):
                        for row in read_csv_rows(path):
                            mark("codeable", keys, page_num_from_row(row))
                    elif name.endswith("_encounter.csv"):
                        for row in read_csv_rows(path):
                            mark("encounter", keys, page_num_from_row(row))
                    elif name.endswith("_sequencing.csv"):
                        for row in read_csv_rows(path):
                            mark("sequencing", keys, page_num_from_row(row))
                    elif name.endswith("_member_extraction.csv"):
                        for row in read_csv_rows(path):
                            mark("member", keys, page_num_from_row(row))
                    elif name.endswith("_member_verification.csv"):
                        for row in read_csv_rows(path):
                            mark("member", keys, None)

        # Ensure prefix/full-id aliases share stream membership
        for key in list(streams.keys()):
            cid = chart_id_key(key)
            if cid != key and cid in streams:
                streams[key] |= streams[cid]
                streams[cid] |= streams[key]
            for other in list(streams.keys()):
                if other != key and _chart_row_matches(other, key):
                    streams[key] |= streams[other]

        self._pipeline_streams = streams
        self._pipeline_pages = pages
        self._cache_stamp = (time.monotonic(), self._scan_signature())
        return streams, pages

    def _dummy_manifest(self) -> ImagingManifestDetails:
        return ImagingManifestDetails(
            member=None,
            dob=None,
            memberId=None,
        )

    def _manifest_for_folder(self, folder_id: str) -> ImagingManifestDetails:
        """DATA_MODE=local: read from 06-postgres-db/manifest metadata_Rn_Bn CSVs."""
        if self.metadata_root is not None:
            found = manifest_for_record(self.metadata_root, folder_id)
            if found is not None:
                return found
        return self._dummy_manifest()

    def _empty_imaging_pages(
        self, folder_dir: Path, pages: list[tuple[int, Path]]
    ) -> list[ImagingPageResult]:
        """Page skeletons only — extraction fields filled only from pipeline CSVs."""
        from app.services.imaging_overlays import empty_imaging_pages

        return empty_imaging_pages(pages)

    def _parse_imaging_manifest(self, data: Any, folder_id: str) -> ImagingManifestDetails:
        # Prefer CSV metadata (local mode); fall back to JSON embedded manifest, then empty
        from_csv = self._manifest_for_folder(folder_id)
        if from_csv.member or from_csv.dob or from_csv.memberId:
            return from_csv
        if isinstance(data, dict):
            raw = data.get("manifest")
            if isinstance(raw, dict):
                try:
                    return ImagingManifestDetails.model_validate(raw)
                except Exception:
                    pass
        return from_csv

    def _parse_imaging_pages(self, data: Any, folder_dir: Path) -> list[ImagingPageResult]:
        raw_pages = data.get("pages") if isinstance(data, dict) else data
        if not isinstance(raw_pages, list):
            return []
        parsed: list[ImagingPageResult] = []
        for item in raw_pages:
            if not isinstance(item, dict):
                continue
            try:
                parsed.append(ImagingPageResult.model_validate(item))
            except Exception:
                continue
        if parsed:
            return parsed
        return self._empty_imaging_pages(folder_dir, self._page_files(folder_dir))

    def _page_files(self, folder_dir: Path) -> list[tuple[int, Path]]:
        """Collect page images from pages/ (preferred) or folder root as fallback.

        Folders that have images but no OCR yet often keep files at the folder root
        until organize_pages.py moves them into pages/.
        """
        candidates: list[Path] = []
        pages_dir = folder_dir / "pages"
        if pages_dir.is_dir():
            candidates.extend(
                entry
                for entry in pages_dir.iterdir()
                if entry.is_file() and not entry.name.startswith("._") and IMAGE_RE.search(entry.name)
            )

        if not candidates:
            skip_names = {"ocr", "pages"}
            for entry in folder_dir.iterdir():
                if not entry.is_file() or entry.name.startswith("._"):
                    continue
                if entry.name.lower() in skip_names:
                    continue
                if IMAGE_RE.search(entry.name):
                    candidates.append(entry)

        numbered: list[tuple[int, Path]] = []
        other: list[Path] = []
        for entry in candidates:
            num = _page_num_from_name(entry.name)
            if num is not None:
                numbered.append((num, entry))
            else:
                other.append(entry)
        numbered.sort(key=lambda x: x[0])
        next_num = (numbered[-1][0] + 1) if numbered else 1
        other.sort(key=lambda p: p.name.lower())
        for path in other:
            numbered.append((next_num, path))
            next_num += 1
        return numbered

    def _ocr_processed_count(self, folder_dir: Path, page_count: int) -> int:
        """Count pages as OCR-processed only when at least one OCR artifact exists."""
        if page_count == 0:
            return 0
        if any(self._has_ocr(folder_dir, kind) for kind in OCR_KINDS):
            return page_count
        return 0

    def _ocr_status(
        self,
        folder_dir: Path,
        *,
        imaging_processed: int = 0,
        page_count: int = 0,
    ) -> OcrRunStatus:
        """Folder status for History.

        OCR gates imaging: if any of the 3 OCR outputs is missing, status is
        OCR in Progress (or Queued) — never Imaging *, even if pipeline CSVs exist.

        - All 3 OCR + rotation + DOS + member → Imaging Completed (HW optional)
        - All 3 OCR + any of HW / rotation / DOS / member → Imaging in Progress
        - All 3 OCR, no imaging → OCR Completed
        - Partial OCR → OCR in Progress
        - No OCR → Queued
        """
        present = sum(1 for kind in OCR_KINDS if self._has_ocr(folder_dir, kind))
        if present < len(OCR_KINDS):
            return "IN_PROGRESS" if present > 0 else "QUEUED"

        streams = self._pipeline_stream_set(folder_dir.name)
        if self._IMAGING_COMPLETE_STREAMS.issubset(streams):
            return "IMAGING_COMPLETED"
        if streams or imaging_processed > 0:
            return "IMAGING_IN_PROGRESS"
        return "COMPLETED"

    def _touch_paths(self, folder_dir: Path, pages: list[tuple[int, Path]]) -> list[Path]:
        paths = [folder_dir, *(p for _, p in pages)]
        for kind in OCR_KINDS:
            ocr = self._ocr_path(folder_dir, kind)
            if ocr.is_file():
                paths.append(ocr)
        imaging = self._imaging_path(folder_dir)
        if imaging.is_file():
            paths.append(imaging)
        status_file = folder_dir / "ocr" / "ocr_run_status.txt"
        if status_file.is_file():
            paths.append(status_file)
        return paths

    def list_folders(self) -> list[FolderSummary]:
        if not self.data_root.is_dir():
            return []

        summaries: list[FolderSummary] = []
        for entry in sorted(self.data_root.iterdir(), key=lambda p: p.name.lower()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            pages = self._page_files(entry)
            page_count = len(pages)
            imaging_processed = self._imaging_processed_count(entry, page_count)
            run_id, batch_id = run_batch_for_record(self.metadata_root, entry.name)
            summaries.append(
                FolderSummary(
                    id=entry.name,
                    name=entry.name,
                    page_count=page_count,
                    ocr_processed=self._ocr_processed_count(entry, page_count),
                    imaging_processed=imaging_processed,
                    ocr_status=self._ocr_status(
                        entry,
                        imaging_processed=imaging_processed,
                        page_count=page_count,
                    ),
                    last_updated_at=_latest_mtime(self._touch_paths(entry, pages)),
                    run_id=run_id,
                    batch_id=batch_id,
                )
            )
        summaries.sort(
            key=lambda f: f.last_updated_at or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        return summaries

    def get_folder(self, folder_id: str) -> FolderDetail:
        folder_dir = self._folder_dir(folder_id)
        pages = self._page_files(folder_dir)
        has_prelim = self._has_ocr(folder_dir, "preliminary")
        has_final1 = self._has_ocr(folder_dir, "final1")
        has_final2 = self._has_ocr(folder_dir, "final2")
        ready = self._pipeline_page_set(folder_dir.name)
        imaging_full = self._imaging_is_full(folder_dir.name)
        page_summaries = [
            PageSummary(
                page_number=num,
                filename=path.name,
                image_url=f"/api/folders/{folder_id}/pages/{num}/image",
                has_preliminary_ocr=has_prelim,
                has_final1_ocr=has_final1,
                has_final2_ocr=has_final2,
                has_imaging=imaging_full
                or self._page_has_pipeline_data(num, path.name, ready),
            )
            for num, path in pages
        ]
        imaging_processed = (
            len(pages)
            if imaging_full
            else sum(1 for p in page_summaries if p.has_imaging)
        )
        run_id, batch_id = run_batch_for_record(self.metadata_root, folder_id)
        return FolderDetail(
            id=folder_id,
            name=folder_dir.name,
            page_count=len(pages),
            ocr_processed=self._ocr_processed_count(folder_dir, len(pages)),
            imaging_processed=imaging_processed,
            ocr_status=self._ocr_status(
                folder_dir,
                imaging_processed=imaging_processed,
                page_count=len(pages),
            ),
            last_updated_at=_latest_mtime(self._touch_paths(folder_dir, pages)),
            run_id=run_id,
            batch_id=batch_id,
            pages=page_summaries,
        )

    def get_page_image_path(self, folder_id: str, page_number: int) -> Path:
        folder_dir = self._folder_dir(folder_id)
        for num, path in self._page_files(folder_dir):
            if num != page_number:
                continue
            # Prefer corrected-pages (incl. TIFF→JPG as {stem}.jpg).
            corrected_dir = folder_dir / "corrected-pages"
            if corrected_dir.is_dir():
                for name in (f"{path.stem}.jpg", path.name):
                    candidate = corrected_dir / name
                    if candidate.is_file() and candidate.stat().st_size > 0:
                        return candidate
            return path
        raise HTTPException(status_code=404, detail=f"Page {page_number} not found in {folder_id}")

    def get_ocr_text(self, folder_id: str, kind: str) -> OcrTextResponse:
        normalized = _normalize_kind(kind)
        folder_dir = self._folder_dir(folder_id)
        path = self._ocr_path(folder_dir, normalized)
        if not path.is_file():
            expected = f"{folder_dir.name}_{KIND_TO_SUFFIX[normalized]}"
            raise HTTPException(
                status_code=404,
                detail=f"No {normalized} OCR file ({expected}.json/.txt) in {folder_id}",
            )

        raw = path.read_text(encoding="utf-8", errors="replace")
        headers_by_file: dict[str, list[OcrSectionHeader]] = {}
        if path.suffix.lower() == ".json":
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise HTTPException(
                    status_code=422,
                    detail=f"Invalid OCR JSON in {path.name}: {exc}",
                ) from exc
            text = azdoc_json_to_ocr_text(data)
            headers_by_file = section_headers_by_file_from_ocr_json(
                data, folder_dir=folder_dir
            )
        else:
            text = raw

        return OcrTextResponse(
            folder_id=folder_id,
            kind=normalized,
            text=text,
            section_headers_by_file=headers_by_file,
        )

    def _dos_overlay_for_folder(self, folder_dir: Path) -> dict[str, dict[str, str | None]]:
        """Prefer imaging/<chart>_dos.csv, else combined dos_extraction.csv for this chart."""
        from app.services.imaging_overlays import resolve_pipeline_csv

        chart = folder_dir.name
        per_chart = folder_dir / "imaging" / f"{chart}_dos.csv"
        rows = _read_dos_csv_rows(per_chart)
        if not rows:
            combined = resolve_pipeline_csv(
                self.data_root,
                "02-imaging-pipeline",
                "dos-extraction",
                "output",
                "dos_extraction.csv",
            )
            rows = [
                r
                for r in _read_dos_csv_rows(combined)
                if (r.get("chart_name") or r.get("chart_id") or "").strip() == chart
                or chart.startswith((r.get("chart_id") or r.get("chart_name") or "") + "_")
            ]
        return _index_dos_rows(rows, chart)

    def _hw_overlay_for_folder(self, folder_dir: Path) -> dict[str, dict[str, Any]]:
        """Prefer imaging/<chart>_hw_printed.csv, else hw_printed.csv."""
        from app.services.imaging_overlays import resolve_pipeline_csv

        chart = folder_dir.name
        per_chart = folder_dir / "imaging" / f"{chart}_hw_printed.csv"
        rows = _read_dos_csv_rows(per_chart)
        if not rows:
            combined = resolve_pipeline_csv(
                self.data_root, "01-ocr-extraction", "output", "hw_printed.csv"
            )
            rows = [
                r
                for r in _read_dos_csv_rows(combined)
                if (r.get("chart_name") or r.get("chart_id") or "").strip() == chart
                or chart.startswith((r.get("chart_id") or r.get("chart_name") or "") + "_")
            ]
        return _index_hw_rows(rows, chart)

    def get_imaging(self, folder_id: str) -> ImagingDocumentResponse:
        """Build imaging rows from pipeline CSVs only (no dummy fabricated values)."""
        from app.services.imaging_overlays import (
            collect_rows,
            empty_imaging_pages,
            index_dos_rows,
            index_hw_rows,
            index_junk_rows,
            index_codeable_rows,
            index_encounter_rows,
            index_sequencing_rows,
            index_member_extraction_rows,
            index_quality_rows,
            index_rotation_rows,
            load_verification,
            load_verifications,
            overlay_fields,
            read_csv_rows,
            monorepo_root_from_data,
        )

        folder_dir = self._folder_dir(folder_id)
        pages = self._page_files(folder_dir)
        path = self._imaging_path(folder_dir)
        chart = folder_dir.name

        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            except json.JSONDecodeError as exc:
                raise HTTPException(
                    status_code=422,
                    detail=f"Invalid imaging JSON in {path.name}: {exc}",
                ) from exc
            imaging_pages = self._parse_imaging_pages(data, folder_dir)
            manifest = self._parse_imaging_manifest(data, folder_id)
        else:
            imaging_pages = empty_imaging_pages(pages)
            manifest = self._manifest_for_folder(folder_id)

        dos_rows = collect_rows(
            folder_dir=folder_dir,
            data_root=self.data_root,
            per_chart_name=f"{chart}_dos.csv",
            combined_rel=(
                "02-imaging-pipeline",
                "dos-extraction",
                "output",
                "dos_extraction.csv",
            ),
            chart_name=chart,
        )
        hw_rows = collect_rows(
            folder_dir=folder_dir,
            data_root=self.data_root,
            per_chart_name=f"{chart}_hw_printed.csv",
            combined_rel=("01-ocr-extraction", "output", "hw_printed.csv"),
            chart_name=chart,
        )
        rotation_rows = collect_rows(
            folder_dir=folder_dir,
            data_root=self.data_root,
            per_chart_name=f"{chart}_rotation.csv",
            combined_rel=(
                "02-imaging-pipeline",
                "rotation-orientation",
                "output",
                "rotation.csv",
            ),
            chart_name=chart,
        )
        quality_rows = collect_rows(
            folder_dir=folder_dir,
            data_root=self.data_root,
            per_chart_name=f"{chart}_quality.csv",
            combined_rel=None,
            chart_name=chart,
        )
        member_rows = collect_rows(
            folder_dir=folder_dir,
            data_root=self.data_root,
            per_chart_name=f"{chart}_member_extraction.csv",
            combined_rel=(
                "02-imaging-pipeline",
                "member-verification",
                "output",
                "member_extraction_results.csv",
            ),
            chart_name=chart,
        )
        junk_rows = collect_rows(
            folder_dir=folder_dir,
            data_root=self.data_root,
            per_chart_name=f"{chart}_junk.csv",
            combined_rel=(
                "02-imaging-pipeline",
                "junk-classification",
                "output",
                "junk_classification.csv",
            ),
            chart_name=chart,
        )
        codeable_rows = collect_rows(
            folder_dir=folder_dir,
            data_root=self.data_root,
            per_chart_name=f"{chart}_codeable.csv",
            combined_rel=None,
            chart_name=chart,
        )
        encounter_rows = collect_rows(
            folder_dir=folder_dir,
            data_root=self.data_root,
            per_chart_name=f"{chart}_encounter.csv",
            combined_rel=None,
            chart_name=chart,
        )
        sequencing_rows = collect_rows(
            folder_dir=folder_dir,
            data_root=self.data_root,
            per_chart_name=f"{chart}_sequencing.csv",
            combined_rel=None,
            chart_name=chart,
        )

        imaging_pages = overlay_fields(imaging_pages, index_dos_rows(dos_rows, chart))
        imaging_pages = overlay_fields(imaging_pages, index_hw_rows(hw_rows, chart))
        imaging_pages = overlay_fields(
            imaging_pages, index_rotation_rows(rotation_rows, chart)
        )
        imaging_pages = overlay_fields(
            imaging_pages, index_quality_rows(quality_rows, chart)
        )
        imaging_pages = overlay_fields(
            imaging_pages, index_member_extraction_rows(member_rows, chart)
        )
        imaging_pages = overlay_fields(imaging_pages, index_junk_rows(junk_rows, chart))
        imaging_pages = overlay_fields(
            imaging_pages, index_codeable_rows(codeable_rows, chart)
        )
        imaging_pages = overlay_fields(
            imaging_pages, index_encounter_rows(encounter_rows, chart)
        )
        imaging_pages = overlay_fields(
            imaging_pages, index_sequencing_rows(sequencing_rows, chart)
        )

        ver_rows = collect_rows(
            folder_dir=folder_dir,
            data_root=self.data_root,
            per_chart_name=f"{chart}_member_verification.csv",
            combined_rel=(
                "02-imaging-pipeline",
                "member-verification",
                "output",
                "member_verification_summary.csv",
            ),
            chart_name=chart,
        )
        if not ver_rows:
            alt = (
                monorepo_root_from_data(self.data_root)
                / "02-imaging-pipeline"
                / "member-verification"
                / "output"
                / "member_verification_summary.csv"
            )
            ver_rows = [
                r
                for r in read_csv_rows(alt)
                if (r.get("chart_id") or r.get("chart_name") or "").strip()
                in {chart, chart.split("_", 1)[0]}
                or chart.startswith((r.get("chart_id") or "") + "_")
            ]

        verification = load_verification(ver_rows, chart)
        verifications = load_verifications(ver_rows, chart)

        sections = ImagingSectionsProcessed(
            member=bool(member_rows),
            dos=bool(dos_rows),
            hw=bool(hw_rows),
            quality=bool(quality_rows),
            rotation=bool(rotation_rows),
            junk=bool(junk_rows),
            codeable=bool(codeable_rows),
            encounter=bool(encounter_rows),
            sequencing=bool(sequencing_rows),
            verification=bool(ver_rows),
        )

        return ImagingDocumentResponse(
            folder_id=folder_id,
            manifest=manifest,
            verification=verification,
            verifications=verifications,
            pages=imaging_pages,
            sectionsProcessed=sections,
        )
