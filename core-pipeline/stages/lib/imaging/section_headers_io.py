"""Derive ``section_headers`` from on-disk Final1 / Final2 OCR JSON.

OCR stores raw candidates (or enough structure to rebuild them). Canon matching
lives here so editing ``section_header_canon.json`` or the matcher only needs
this stage — not a re-OCR.
"""
from __future__ import annotations

from typing import Any, Optional

def _canonize_kept(kept: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Prefer the matched catalog label as ``text`` for overlays."""
    out: list[dict[str, Any]] = []
    for item in kept:
        enriched = dict(item)
        canon = str(enriched.get("matched_canonical") or "").strip()
        if canon:
            enriched["text"] = canon
        out.append(enriched)
    return out


def apply_header_filter(
    candidates: list[dict[str, Any]],
    *,
    use_minilm: bool = True,
) -> list[dict[str, Any]]:
    """Run the shared canon filter and stamp matched labels onto ``text``."""
    from stages.lib.imaging.section_header_match import filter_section_headers

    kept = filter_section_headers(candidates, use_minilm=use_minilm)
    return _canonize_kept(kept)


def candidates_from_azure_pages_meta(
    page: dict[str, Any],
    *,
    image_size: Optional[tuple[float, float]] = None,
) -> list[dict[str, Any]]:
    """Rebuild Final2 header candidates from stored ``pagesMeta[].lines``."""
    from stages.ocr_final2_azure import candidates_from_lines

    pages_meta = page.get("pagesMeta") or page.get("pages_meta") or []
    if not isinstance(pages_meta, list):
        return []
    out: list[dict[str, Any]] = []
    for meta in pages_meta:
        if not isinstance(meta, dict):
            continue
        try:
            pw = float(meta.get("width") or 0)
            ph = float(meta.get("height") or 0)
        except (TypeError, ValueError):
            pw = ph = 0.0
        unit = meta.get("unit")
        if unit is not None and hasattr(unit, "value"):
            unit = getattr(unit, "value", unit)
        out.extend(
            candidates_from_lines(
                list(meta.get("lines") or []),
                page_w=pw,
                page_h=ph,
                unit=str(unit) if unit is not None else None,
                image_size=image_size,
            )
        )
    return out


def candidates_from_docling_document(document: Any) -> list[dict[str, Any]]:
    """Rebuild Final1 candidates from an exported Docling ``document`` dict."""
    if not isinstance(document, dict):
        return []
    from stages.lib.imaging.docling_ocr import extract_section_headers_from_dict

    return extract_section_headers_from_dict(document)


def candidates_from_page(
    page: dict[str, Any],
    *,
    kind: str,
    image_size: Optional[tuple[float, float]] = None,
) -> list[dict[str, Any]]:
    """Best available candidate list for one OCR page object.

    Prefer explicit ``section_header_candidates`` (written by OCR). Else rebuild
    from Final2 ``pagesMeta`` lines or Final1 ``document``. Last resort: the
    already-filtered ``section_headers`` (re-score only — cannot recover drops).
    """
    raw = (
        page.get("section_header_candidates")
        or page.get("sectionHeaderCandidates")
    )
    if isinstance(raw, list) and raw:
        return [c for c in raw if isinstance(c, dict)]

    kind_l = (kind or "").strip().casefold()
    if kind_l in {"final2", "azure", "azuredocintel"} or (
        page.get("pagesMeta") or page.get("pages_meta")
    ):
        rebuilt = candidates_from_azure_pages_meta(page, image_size=image_size)
        if rebuilt:
            return rebuilt

    document = page.get("document")
    if document:
        rebuilt = candidates_from_docling_document(document)
        if rebuilt:
            return rebuilt

    existing = page.get("section_headers") or page.get("sectionHeaders") or []
    if isinstance(existing, list):
        return [c for c in existing if isinstance(c, dict)]
    return []


def refresh_page_headers(
    page: dict[str, Any],
    *,
    kind: str,
    use_minilm: bool = True,
    image_size: Optional[tuple[float, float]] = None,
) -> dict[str, Any]:
    """Return a copy of ``page`` with ``section_headers`` re-derived in place.

    Also persists ``section_header_candidates`` when they were rebuilt so the
    next re-run does not need ``pagesMeta`` / ``document`` again.
    """
    out = dict(page)
    candidates = candidates_from_page(out, kind=kind, image_size=image_size)
    out["section_header_candidates"] = candidates
    out["section_headers"] = apply_header_filter(candidates, use_minilm=use_minilm)
    return out


def refresh_ocr_json_doc(
    doc: dict[str, Any],
    *,
    kind: str,
    use_minilm: bool = True,
) -> tuple[dict[str, Any], int, int]:
    """Refresh every page in a Final1/Final2 JSON document.

    Returns ``(new_doc, pages_touched, headers_kept)``.
    """
    pages_in = list(doc.get("pages") or [])
    pages_out: list[dict[str, Any]] = []
    headers_kept = 0
    for page in pages_in:
        if not isinstance(page, dict):
            continue
        refreshed = refresh_page_headers(page, kind=kind, use_minilm=use_minilm)
        headers_kept += len(refreshed.get("section_headers") or [])
        pages_out.append(refreshed)
    new_doc = dict(doc)
    new_doc["pages"] = pages_out
    new_doc["pageCount"] = len(pages_out)
    return new_doc, len(pages_out), headers_kept


def page_has_ocr_payload(page: dict[str, Any]) -> bool:
    """True when the page JSON has something we can derive headers from."""
    if page.get("section_header_candidates") or page.get("sectionHeaderCandidates"):
        return True
    if page.get("pagesMeta") or page.get("pages_meta"):
        return True
    if page.get("document"):
        return True
    if page.get("section_headers") or page.get("sectionHeaders"):
        return True
    content = str(page.get("content") or page.get("markdown") or "").strip()
    return bool(content)
