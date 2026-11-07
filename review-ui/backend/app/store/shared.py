"""Helpers the Postgres repository uses for OCR display: section headers (matched against
core-pipeline's canon), OCR text clean-up, skip messages, DOS formatting. What was left of
the Local mode repository once review-ui read only the database (2026-11-07)."""
from __future__ import annotations
import json
import logging
import re
import sys
import threading
from datetime import date
from pathlib import Path
from typing import Any
from fastapi import HTTPException
from app.services.dates import show_date
from app.core.schemas import (
    OcrKind,
    OcrSectionHeader,
)


# API kind → filename suffix: <folder>_<suffix>.{txt|json}
KIND_TO_SUFFIX: dict[str, str] = {
    "preliminary": "prelim",
    "final1": "final1",
    "final2": "final2",
}


def _fmt_dos_display(raw: str | date | None) -> str | None:
    """Normalize dates to YYYY-MM-DD for the Imaging UI."""
    return show_date(raw)


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
    out.sort(key=lambda header: (header.width <= 0 or header.height <= 0, header.top, header.left))
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
    # review-ui/backend/app/store/local → parents[4] = monorepo
    here = Path(__file__).resolve()
    for parent in here.parents:
        core = parent / "core-pipeline"
        if core.is_dir() and (core / "modules" / "ocr").is_dir():
            return core
    return None


_HEADER_MATCH_THRESHOLD = 0.90


# (canon mtime, header text) → canonical label, or "" when below threshold.
# A chart's Final OCR repeats the same few hundred header strings across every
# page; scoring each against the whole canon list on every OCR fetch was the
# bulk of /ocr latency. The mtime in the key keeps list edits live.
_header_match_cache: dict[tuple[float | None, str], str] = {}


_header_match_lock = threading.Lock()


_header_matcher: Any = None  # modules.ocr.section_header_match, once imported


def _load_header_matcher() -> Any:
    global _header_matcher
    if _header_matcher is not None:
        return _header_matcher
    core = _core_pipeline_dir()
    if core is None:
        return None
    core_s = str(core)
    if core_s not in sys.path:
        sys.path.insert(0, core_s)
    from modules.ocr import section_header_match  # type: ignore

    _header_matcher = section_header_match
    return _header_matcher


def _canon_label_for(matcher: Any, text: str) -> str:
    try:
        mtime: float | None = matcher.canon_path().stat().st_mtime
    except OSError:
        mtime = None
    key = (mtime, text)
    label = _header_match_cache.get(key)
    if label is None:
        score, canonical = matcher.best_header_match(text, use_minilm=False)
        label = canonical if score >= _HEADER_MATCH_THRESHOLD else ""
        with _header_match_lock:
            if len(_header_match_cache) > 50_000:
                _header_match_cache.clear()
            _header_match_cache[key] = label
    return label


def _filter_headers_against_canon(
    headers: list[OcrSectionHeader],
) -> list[OcrSectionHeader]:
    """Keep headers with ≥90% lexical match to ``section_header_canon.json``.

    Re-runs on every OCR fetch so editing the list updates overlays without
    re-OCR (results are memoised per canon mtime). Falls back to exact
    normalized match if the matcher cannot import.
    """
    if not headers:
        return headers
    try:
        matcher = _load_header_matcher()
    except Exception as exc:
        matcher = None
        logging.getLogger(__name__).warning(
            "Live section-header canon filter skipped: %s", exc
        )
    if matcher is not None:
        out: list[OcrSectionHeader] = []
        for h in headers:
            text = (h.text or "").strip()
            if not text:
                continue
            # Prefer the canon label so OCR noise like "4 Allergies" → "Allergies".
            label = _canon_label_for(matcher, text)
            if label:
                out.append(h.model_copy(update={"text": label}))
        return out

    # Exact-match fallback against the JSON list (no MiniLM / difflib).
    core = _core_pipeline_dir()
    canon_path = None
    if core is not None:
        canon_path = core / "resources" / "canon" / "section_header_canon.json"
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

