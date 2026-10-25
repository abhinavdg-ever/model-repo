"""The Final page type, per continuity document: ``page_arbitration.json``.

The **Extracted** page type is the page axes (``axes.py``), decided from the
page alone. The **Final** page type (``decide_final``, run by
``imaging_final`` on the continuity stage's documents) re-places each page for
the document it sits in. The **family** (L1) is agreed across a document's
pages; each page keeps its own names below it.

  The agreed family (``agree_document_type``), first that applies:
    first_page     the first page's L1, whenever it has one (Unknown and Others
                   — neither the model nor the canon was sure — do not count)
    vote           else the pages' L1s, weighted by page-type confidence, when
                   one holds at least ``document_agreement.vote_min_share``
                   (0.60) of the weight; a tie goes to the first page's
                   (flagged for review when it changes a page)
    (none)         no clear majority: pages keep their own, flagged for review
                   (``no_agreement``)

  A page of the agreed family keeps its own page type and sub-type. A page of
  another family takes the agreed L1 as its page type, with the L2
  ``final_placement`` gives its own L1 there (a Lab Report page inside a
  Progress Note: Progress Note / Lab and Imaging Results); not listed, inside
  a Progress Note: Others; elsewhere the L2 the page classifier gave the page
  under that L1 (``under_l1``), else its own name when that L1 has it, else
  Others. An Others page is placed the same way. Never L1 / L1.

  Codability comes from the **Final** page type, never from the page's own
  (Extracted) answer (2026-10-25, user): the codeability tag of the Final path in
  ``page_taxonomy.json`` → ``codeability`` (its deepest level's; an L1 alone reads its
  Others L2). A path tagged needs_review takes the document's codability (its first
  page's Final tag) when that is decided, else stays Non-Codable flagged for review.
  One exception, kept from 2026-10-24: a page re-placed into a Progress Note that
  carries an ``inside_progress_note.non_codeable_names`` name (Lab Result, Medication
  Record…) stays Non-Codable. A page with no Final L1 (Others, Unknown) keeps its own.

  A page in no document (or a one-page document) keeps its own answer, except
  embedded_same_page: a page of an ``embedded_families`` L1 (Lab Report)
  headed by a Progress Note title → Progress Note / its placement there.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from stages.lib.canon_store import CANON_DIR, CanonFile
from stages.lib.page_classify import axes
from stages.lib.page_classify import taxonomy as tax
from stages.lib.page_classify.keywords import KeywordResult

_ARBITRATION: CanonFile[dict[str, Any]] = CanonFile(CANON_DIR / "page_arbitration.json")


UNKNOWN = "Unknown"
OTHERS = axes.OTHERS


def keyword_confidence(keyword: Optional[KeywordResult]) -> Optional[float]:
    """The keyword canon's own confidence: a base for a title hit or body terms,
    plus a step per point of margin over the next candidate, capped."""
    if keyword is None:
        return None
    c = _ARBITRATION.get()["confidence"]
    if keyword.title_hit:
        base, cap = float(c["keyword_title_base"]), float(c["keyword_title_cap"])
    else:
        base, cap = float(c["keyword_body_base"]), float(c["keyword_body_cap"])
    return round(min(cap, base + float(c["keyword_per_margin"]) * max(0.0, keyword.margin)), 4)


# --- the Final answer ---------------------------------------------

# classification_category → codability label (imaging_final.codability).
CODABILITY_OF = {category: codability for codability, category in tax.CATEGORY.items()}


@dataclass(frozen=True)
class FinalPage:
    """What level 3 needs about one page (from page_classification + continuity)."""

    page_id: Any
    page_type: Optional[str]  # as written: L1, or "L1 (L2)"
    page_subtype: Optional[str]
    family: Optional[str] = None  # L1; None reads page_type
    names: tuple[str, ...] = ()  # canon page type, L3, L2, L1 (axes.names_of)
    l2: Optional[str] = None  # its own L2 (axes.l2_of)
    under_l1: dict[str, Any] = field(default_factory=dict)  # axes.under_l1_of
    category: Optional[str] = None  # its own classification_category (from the canon)
    codeability_rule: Optional[str] = None  # page_type_tag | context | source
    title_l1: Optional[str] = None  # the L1 whose canon title heads the page
    model_type: Optional[str] = None
    document_seq: Optional[int] = None
    seq: Optional[int] = None  # position within the document; 1 = start
    link_strength: Optional[str] = None  # strong | weak (chain from the start)
    # The page type's confidence (page_classification.confidence): its vote weight.
    confidence: Optional[float] = None


@dataclass(frozen=True)
class FinalResult:
    page_type: Optional[str]
    page_subtype: Optional[str]
    model_type: Optional[str]
    codability: Optional[str]
    source: str  # page | continuation | embedded
    continuation_rule: str
    needs_review: bool


def _family(page: FinalPage) -> Optional[str]:
    return page.family or page.page_type


def _known(name: Optional[str]) -> bool:
    return name not in (None, "", UNKNOWN)


def _agreeable(name: Optional[str]) -> bool:
    """An L1 a document can agree on: not Unknown, and not Others (a page neither
    the model nor the canon was sure of takes its document's L1 instead)."""
    return _known(name) and name != OTHERS


def _placement(agreed: str, family: Optional[str]) -> Optional[str]:
    """``final_placement[agreed][family]``: the L2 a page of ``family`` takes
    inside a document of ``agreed``."""
    table = _ARBITRATION.get().get("final_placement") or {}
    found = (table.get(agreed) or {}).get(family or "")
    return found if isinstance(found, str) and found else None


def _note_rules() -> dict[str, Any]:
    return _ARBITRATION.get().get("inside_progress_note") or {}


def _section_l1() -> str:
    return (_ARBITRATION.get().get("axes") or {}).get("section_L1", tax.PROGRESS_NOTE)


def _inside_note_non_codable(page: FinalPage) -> bool:
    listed = set(_note_rules().get("non_codeable_names") or ())
    return any(name in listed for name in (page.names or (page.page_subtype, page.page_type)))


_CATEGORY_OF_TAG = {"codeable": "codeable", "non_codeable": "non_codeable", "discharge": "discharge_summary"}


def path_of(page_type: Optional[str], page_subtype: Optional[str]) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """(L1, L2, L3) from ``page_type`` / ``page_subtype`` as written: "L1 (L2)" / L3 or
    L1 / L2."""
    if not _known(page_type):
        return None, None, None
    found = re.fullmatch(r"(.+?) \((.+)\)", page_type or "")
    if found:
        return found.group(1), found.group(2), page_subtype or None
    return page_type, page_subtype or None, None


def final_tag(page_type: Optional[str], page_subtype: Optional[str]) -> Optional[str]:
    """The codeability tag of a Final page type; None when it has no L1."""
    return tax.codeability_of(*path_of(page_type, page_subtype))


def _final_codability(page: FinalPage, page_type: Optional[str], page_subtype: Optional[str],
                      document_tag: Optional[str] = None) -> tuple[Optional[str], bool]:
    """(codability label, needs review) of a page whose Final type is ``page_type`` /
    ``page_subtype``: the Final path's tag; needs_review takes ``document_tag`` when that
    is decided. A page re-placed into a Progress Note that carries a non_codeable_names
    name stays Non-Codable. No Final L1: the page's own category."""
    l1 = path_of(page_type, page_subtype)[0]
    if l1 == _section_l1() and _family(page) != l1 and _inside_note_non_codable(page):
        return CODABILITY_OF["non_codeable"], False
    tag = final_tag(page_type, page_subtype)
    if tag is None:
        return CODABILITY_OF.get(page.category or ""), False
    if tag == "needs_review" and document_tag in _CATEGORY_OF_TAG:
        tag = document_tag
    if tag == "needs_review":
        return CODABILITY_OF["non_codeable"], True
    return CODABILITY_OF[_CATEGORY_OF_TAG[tag]], False


def _names_under(agreed: str, page: FinalPage) -> tuple[str, str]:
    """``page_type`` / ``page_subtype`` for a page bound into a document of the
    ``agreed`` family, so L1, L2 and L3 always agree with the taxonomy:

    * ``final_placement[agreed][its own L1]`` (inside a Progress Note, a Lab
      Report page is Progress Note / Lab and Imaging Results);
    * inside a Progress Note otherwise Others (its L2 is the content section);
    * elsewhere, the L2 (and L3) the page classifier gave the page under that
      L1 (``under_l1``: the model's, else the canon's) -- "L1 (L2)" / L3 or
      L1 / L2;
    * its own name, when the taxonomy places it under that L1;
    * otherwise L1 / Others.
    """
    placed = _placement(agreed, _family(page))
    if placed:
        return agreed, placed
    if agreed == _section_l1():
        return agreed, OTHERS
    there = (page.under_l1 or {}).get(agreed) or {}
    if there.get("l2"):
        return (f"{agreed} ({there['l2']})", there["l3"]) if there.get("l3") else (agreed, there["l2"])
    permitted = axes.l2_names(agreed)
    own = (page.l2, *page.names) if page.names else (page.l2, page.page_subtype, page.page_type)
    return agreed, next((name for name in own if name and name in permitted), OTHERS)


def _keep(page: FinalPage, rule: str, review: bool, document_tag: Optional[str] = None) -> FinalResult:
    # An L1 is never its own sub-type (rows written before page_names said Others).
    repeated = _known(page.page_type) and page.page_subtype == page.page_type
    subtype = OTHERS if repeated else page.page_subtype
    codability, unsure = _final_codability(page, page.page_type, subtype, document_tag)
    return FinalResult(
        page_type=page.page_type,
        page_subtype=subtype,
        model_type=page.model_type,
        codability=codability,
        source="page",
        continuation_rule=rule,
        needs_review=review or unsure,
    )


def agree_document_type(document: Sequence[FinalPage]) -> tuple[Optional[str], str]:
    """The family (L1) a document's pages agree on, and why: first_page (the
    first page's, whenever it has one) / vote (``(None, "")`` for no
    document)."""
    if not document:
        return None, ""
    first = _family(document[0])
    if _agreeable(first):
        return first, "first_page"
    weights: dict[str, float] = {}
    for p in document:
        family = _family(p)
        if _agreeable(family):
            weights[family] = weights.get(family, 0.0) + (p.confidence if p.confidence is not None else 0.5)
    if not weights:
        return None, ""
    best = max(weights.values())
    share = float((_ARBITRATION.get().get("document_agreement") or {}).get("vote_min_share", 0.0))
    if best < share * sum(weights.values()):
        return None, "no_agreement"
    tied = [t for t, w in weights.items() if abs(w - best) < 1e-9]
    if first in tied:
        return first, "vote"
    order = [_family(p) for p in document]
    return min(tied, key=order.index), "vote"


def decide_final(
    page: FinalPage, document: Optional[Sequence[FinalPage]] = None
) -> FinalResult:
    """The Final page type of one page. ``document`` is every page of its
    continuity document in order (None / one page: the page stands alone)."""
    if document and len(document) > 1:
        agreed, basis = agree_document_type(document)
        if basis == "no_agreement":
            return _keep(page, "no_agreement", True)
        if agreed is None:
            return _keep(page, "agreed", False)
        opener = next((p for p in document if _family(p) == agreed), document[0])
        document_tag = final_tag(opener.page_type, opener.page_subtype)
        if _family(page) == agreed:
            return _keep(page, "agreed", False, document_tag if page is not opener else None)
        page_type, page_subtype = _names_under(agreed, page)
        codability, unsure = _final_codability(page, page_type, page_subtype, document_tag)
        return FinalResult(
            page_type=page_type,
            page_subtype=page_subtype,
            model_type=page.model_type,
            codability=codability,
            source="continuation",
            continuation_rule=f"agreed_{basis}",
            needs_review=basis == "vote" or page.link_strength == "weak" or unsure,
        )
    section = _section_l1()
    if _family(page) in set(_note_rules().get("embedded_families") or ()) and page.title_l1 == section:
        page_subtype = _names_under(section, page)[1]
        codability, unsure = _final_codability(page, section, page_subtype)
        return FinalResult(
            page_type=section,
            page_subtype=page_subtype,
            model_type=page.model_type,
            codability=codability,
            source="embedded",
            continuation_rule="embedded_same_page",
            needs_review=unsure,
        )
    return _keep(page, "no_change", False)
