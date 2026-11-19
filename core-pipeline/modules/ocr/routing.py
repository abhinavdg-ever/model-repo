"""Which pages go to Azure Document Intelligence, and each page's Final Quality.

The user's routing table (2026-11-14). One row per page; the row number is stored in
``ocr_quality_results.quality_detail.routing`` and written to ``<chart>_ocr_routing.csv``.

  #   Type         HW %     Quality        Docling vs Tesseract       Final Quality   Doc Intel
  1   Printed      < 10%    High           >= 90%                     High            No
  2   Printed      < 10%    High           < 90%                      Medium          Yes
  3   Printed      < 10%    Medium         >= 95%                     High            No
  4   Printed      < 10%    Medium         < 95%                      Medium          Yes
  5   Printed      < 10%    Low            -                          Low             Yes
  6   Mixed        10-40%   any            -                          3-engine re-grade  Yes
  7   Handwritten  > 40%    any            -                          3-engine re-grade  Yes
  8   Form         any      any            -                          3-engine re-grade  Yes
  9   Visual       < 10%    High / Medium  >= 90%, or < 25 words      High            No
  10  Visual       any other case                                     Medium          Yes
  11  anything else (uncertain, no grade, no Docling / Tesseract text) 3-engine re-grade  Yes
  12  blank / junk / duplicate in pass 1                              model's grade, else Low   No

Rows 5-10 are never decided by blank / junk pass 1 (Tesseract). The 3-engine re-grade
(rows 6, 7, 8, 11) runs after Doc Intel: the average word overlap of Tesseract, Docling and
Doc Intel; < 50% one level down, < 10% Low (``downgrade_by_ocr_overlap``); never up.

2026-11-19 (user), on top of the table:

  * Docling's first / last row is checked against Tesseract's in stage 4 (edge_check.py): a
    row only split or run together differently takes Tesseract's line; a **missing** row caps
    the Docling vs Tesseract match rate just under 90% (``EDGE_MISSING_CAP``), so a Printed
    page is never row 1 / 3 for it: Medium and Doc Intel.
  * A Printed High / Medium page under 50% (``LOW_BELOW``) is Low (rows 2 / 4), Doc Intel.
  * After Doc Intel, a Low page whose three engines agree on 50% or more (``LOW_TO_MEDIUM_AT``,
    three-way overlap) is Medium. ``overlap_3`` is kept for every page Doc Intel read.
  * review-ui's Rejection Rules list a Low page whose final match rate (``overlap_3``, else
    ``overlap``) is under 20% (``REJECT_BELOW``).

Type: the model's Form or Visual wins. Otherwise the handwritten share of the page decides
(2026-11-14, user: over 10% handwriting is never Printed, and a page the model calls
Handwritten with under 10% is Printed). With no share measured, the model's Printed /
Handwritten (the legacy label Mixed is Mixed). Uncertain, Blank or nothing -> row 11.
"""
from __future__ import annotations

import re
from typing import Any, Optional

HW_PRINTED_BELOW = 10.0  # handwritten share (%) under which a page is Printed
HW_HANDWRITTEN_ABOVE = 40.0  # over which it is Handwritten; between: Mixed
KEEP_HIGH_AT = 0.90  # row 1: High stays High (else row 2, Medium, Doc Intel)
MEDIUM_TO_HIGH_AT = 0.95  # row 3: Medium becomes High (else row 4)
VISUAL_KEEP_AT = 0.90  # row 9
VISUAL_FEW_WORDS = 25  # row 9: a visual page with fewer Docling words than this

EDGE_MISSING_CAP = 0.8999  # a missing top / bottom row: the match rate is cut under 90%
LOW_BELOW = 0.50  # Printed High / Medium under this match rate: Low
LOW_TO_MEDIUM_AT = 0.50  # after Doc Intel: a Low page at this three-way rate or more: Medium
REJECT_BELOW = 0.20  # a Low page under this final match rate is listed in Rejection Rules

REGRADE_ROWS = frozenset({6, 7, 8, 11})
PASS2_ONLY_ROWS = frozenset({5, 6, 7, 8, 9, 10})

_WORD = re.compile(r"[a-z0-9]+")


def _num(value: Any) -> Optional[float]:
    try:
        return None if value is None or value == "" else float(value)
    except (TypeError, ValueError):
        return None


def page_kind(row: Optional[dict[str, Any]]) -> str:
    """printed / mixed / handwritten / form / visual / other, for one ocr_quality_results row."""
    if not row:
        return "other"
    doc = str(row.get("document_type") or "").strip().lower()
    legacy = str(row.get("printed_or_handwritten") or "").strip().lower()
    if doc in {"form", "visual"}:
        return doc
    if not doc and legacy == "mixed":
        return "mixed"
    model = doc or legacy
    if model not in {"printed", "handwritten"}:
        return "other"
    hw = _num(row.get("handwritten_area_pct"))
    if hw is None:
        return model
    if hw < HW_PRINTED_BELOW:
        return "printed"
    if hw > HW_HANDWRITTEN_ABOVE:
        return "handwritten"
    return "mixed"


def kind_label(kind: str) -> str:
    return {"other": "Other"}.get(kind, kind.capitalize())


def judged_in_pass2(row: Optional[dict[str, Any]]) -> bool:
    """Rows 5-10 (Printed Low, Mixed, Handwritten, Form, Visual): blank / junk pass 1 does not
    decide them. Not a page the HW stage found no ink on, nor one typed Blank (2026-10-27)."""
    from common.db import NO_INK_HW_METHODS

    if not row or str(row.get("hw_method") or "").lower() in NO_INK_HW_METHODS:
        return False
    kind = page_kind(row)
    if kind in {"mixed", "handwritten", "form", "visual"}:
        return True
    if kind == "printed":
        return str(row.get("quality_tag") or "").strip().lower() == "low"
    # Uncertain pages stay out of pass 1 as before; Blank is judged there.
    doc = str(row.get("document_type") or row.get("printed_or_handwritten") or "").strip().lower()
    return doc == "uncertain"


def word_count(text: Optional[str]) -> int:
    return len(_WORD.findall((text or "").lower()))


def overlap(a: Optional[str], b: Optional[str]) -> Optional[float]:
    from modules.image_preprocess.quality_label_postprocess import ocr_overlap

    return ocr_overlap(a, b)


def three_way_overlap(texts: list[Optional[str]]) -> Optional[float]:
    """Average pairwise word overlap of the engines that have text (2 or more), else None."""
    have = [t for t in texts if (t or "").strip()]
    if len(have) < 2:
        return None
    pairs = [overlap(a, b) for i, a in enumerate(have) for b in have[i + 1:]]
    pairs = [p for p in pairs if p is not None]
    return round(sum(pairs) / len(pairs), 4) if pairs else None


def route(
    row: Optional[dict[str, Any]],
    *,
    quality: Optional[str],
    settled: bool,
    docling: Optional[str],
    tesseract: Optional[str],
    edges: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """The page's table row before Doc Intel: row, kind, final quality (provisional for the
    3-engine rows), whether it goes to Doc Intel, and the overlap used."""
    kind = page_kind(row)
    hw = _num((row or {}).get("handwritten_area_pct"))
    grade = (quality or "").strip().lower() or None
    out: dict[str, Any] = {
        "type": kind_label(kind), "hw_pct": hw, "quality_before": grade,
        "overlap": None, "docling_words": None, "edges": edges,
    }

    def done(row_no: int, final: Optional[str], docintel: bool) -> dict[str, Any]:
        return {**out, "row": row_no, "final_quality": final, "docintel": docintel}

    if settled:
        return done(12, grade or "low", False)
    has_docling, has_tess = bool((docling or "").strip()), bool((tesseract or "").strip())
    if has_docling and has_tess:
        out["overlap"] = overlap(docling, tesseract)
    if has_docling:
        out["docling_words"] = word_count(docling)
    from modules.ocr.edge_check import edge_missing

    if out["overlap"] is not None and edge_missing(edges) and out["overlap"] > EDGE_MISSING_CAP:
        out["overlap_before_edge"] = out["overlap"]
        out["overlap"] = EDGE_MISSING_CAP
    ov = out["overlap"]
    # Under LOW_BELOW a Printed page is Low, still to Doc Intel (2026-11-19, user).
    below = "low" if ov is not None and ov < LOW_BELOW else "medium"

    if kind == "printed" and grade and has_docling and has_tess:
        if grade == "high":
            return done(1, "high", False) if ov is not None and ov >= KEEP_HIGH_AT else done(2, below, True)
        if grade == "medium":
            return done(3, "high", False) if ov is not None and ov >= MEDIUM_TO_HIGH_AT else done(4, below, True)
        return done(5, "low", True)
    if kind == "printed" and grade == "low":
        return done(5, "low", True)
    if kind == "mixed":
        return done(6, grade, True)
    if kind == "handwritten":
        return done(7, grade, True)
    if kind == "form":
        return done(8, grade, True)
    if kind == "visual":
        few = has_docling and (out["docling_words"] or 0) < VISUAL_FEW_WORDS
        if (hw is None or hw < HW_PRINTED_BELOW) and grade in {"high", "medium"} and has_docling and (
                few or (ov is not None and ov >= VISUAL_KEEP_AT)):
            return done(9, "high", False)
        return done(10, "medium", True)
    return done(11, grade, True)


def after_docintel(decision: dict[str, Any], texts: list[Optional[str]]) -> dict[str, Any]:
    """Rows 6, 7, 8, 11: re-grade on the three engines' overlap (< 50% one level down,
    < 10% Low). Then any page Doc Intel read that is Low becomes Medium when the three
    engines agree on LOW_TO_MEDIUM_AT or more (2026-11-19, user)."""
    if not decision.get("docintel"):
        return decision
    ov3 = three_way_overlap(texts)
    out = {**decision, "overlap_3": ov3}
    if decision.get("row") in REGRADE_ROWS:
        from modules.image_preprocess.quality_label_postprocess import downgrade_by_ocr_overlap

        out["final_quality"] = downgrade_by_ocr_overlap(decision.get("quality_before"), ov3)
    if out.get("final_quality") == "low" and ov3 is not None and ov3 >= LOW_TO_MEDIUM_AT:
        out["final_quality"] = "medium"
        out["low_upgraded"] = True
    return out


def final_match_rate(decision: Optional[dict[str, Any]]) -> Optional[float]:
    """The page's last match rate: three engines when Doc Intel read it, else Docling vs Tesseract."""
    if not decision:
        return None
    return decision.get("overlap_3") if decision.get("overlap_3") is not None else decision.get("overlap")


def final_ocr(decision: dict[str, Any], has_azure: bool, has_docling: bool) -> str:
    if decision.get("row") == 12:
        return "-"
    if has_azure:
        return "Doc Intel"
    return "Docling" if has_docling else "Tesseract"
