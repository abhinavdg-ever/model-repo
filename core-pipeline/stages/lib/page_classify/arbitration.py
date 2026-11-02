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
  No exceptions (2026-10-25, user): a lab page re-placed into a Progress Note is
  Progress Note > Lab and Imaging Results, Codable. A page with no Final L1 (Others,
  Unknown) keeps its own.

  Short packets decided by their last page (``last_page_documents``, 2026-10-25, user):
  a document of at most ``max_pages`` pages whose last page is a listed L1 (Consent and
  Authorization: notice / intake pages, then the signed consent) takes that L1 on every
  page; before that, ``packets`` joins such a document with the short documents right
  before it whose pages are all of the listed ``joins`` L1s.

  A page in no document (or a one-page document) keeps its own answer, except
  embedded_same_page: a page of an ``embedded_families`` L1 (Lab Report)
  headed by a Progress Note title → Progress Note / its placement there.
"""
from __future__ import annotations

import json
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
    # visit_document (2026-11-02): the note-section groups headed on the page
    # (note_sections) and whether the page is signed.
    sections: frozenset[str] = frozenset()
    signed: bool = False


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


def _signature_only(page: FinalPage) -> bool:
    """A page classified as only a signature (Progress Note / Signature)."""
    return any((n or "").casefold() == "signature" for n in (page.l2, page.page_subtype, *(page.names or ())))


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
    row = table.get(agreed) or {}
    found = row.get(family or "") or row.get("*")  # "*": any other family (2026-11-01, user)
    # Only an L2 the taxonomy has under that L1: the Final path must stay a valid one.
    return found if isinstance(found, str) and found and found in axes.l2_names(agreed) else None


def _note_rules() -> dict[str, Any]:
    return _ARBITRATION.get().get("inside_progress_note") or {}


def _section_l1() -> str:
    return (_ARBITRATION.get().get("axes") or {}).get("section_L1", tax.PROGRESS_NOTE)


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
    is decided. No Final L1: the page's own category."""
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
      Report page is Progress Note / Lab and Imaging Results), else
      ``final_placement[agreed]["*"]`` (any page that becomes a Clinical Summary is
      Clinical Summary / Summary, 2026-11-01, user);
    * inside a Progress Note otherwise its own name when the Progress Note has it
      (a Summary page: Progress Note / Summary), else Others;
    * elsewhere, the L2 (and L3) the page classifier gave the page under that
      L1 (``under_l1``: the model's, else the canon's) -- "L1 (L2)" / L3 or
      L1 / L2;
    * its own name, when the taxonomy places it under that L1;
    * otherwise L1 / Others.
    """
    placed = _placement(agreed, _family(page))
    if placed:
        return agreed, placed
    permitted = axes.l2_names(agreed)
    own = (page.l2, *page.names) if page.names else (page.l2, page.page_subtype, page.page_type)
    own_name = next((name for name in own if name and name != OTHERS and name in permitted), None)
    if agreed == _section_l1():
        return agreed, own_name or OTHERS
    there = (page.under_l1 or {}).get(agreed) or {}
    if there.get("l2") and there["l2"] in permitted:
        return (f"{agreed} ({there['l2']})", there["l3"]) if there.get("l3") else (agreed, there["l2"])
    return agreed, own_name or OTHERS


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


def _last_page_rules() -> dict[str, Any]:
    return (_ARBITRATION.get().get("last_page_documents") or {}).get("families") or {}


def packets(documents: Sequence[Sequence[FinalPage]]) -> list[list[FinalPage]]:
    """The documents for the Final page type, in chart order: a document whose last page
    is a ``last_page_documents`` L1 absorbs the documents right before it while the
    packet stays within ``max_pages`` and their pages are all of the ``joins`` L1s."""
    rules = _last_page_rules()
    out: list[list[FinalPage]] = []
    for document in documents:
        merged = list(document)
        rule = rules.get(_family(merged[-1]) or "") if merged else None
        while rule and out and len(out[-1]) + len(merged) <= int(rule["max_pages"]) and all(
            _family(p) in rule.get("joins", ()) for p in out[-1]
        ):
            merged = out.pop() + merged
        out.append(merged)
    return out


def agree_document_type(document: Sequence[FinalPage]) -> tuple[Optional[str], str]:
    """The family (L1) a document's pages agree on, and why: last_page (a short packet
    ending in a ``last_page_documents`` L1) / first_page (the first page's, whenever it
    has one) / vote (``(None, "")`` for no document)."""
    if not document:
        return None, ""
    last = _family(document[-1])
    rule = _last_page_rules().get(last or "")
    if rule and len(document) <= int(rule["max_pages"]):
        return last, "last_page"
    # A signature-only first page (it joined the note after it) does not set the family.
    opener = next((p for p in document if not _signature_only(p)), document[0])
    first = _family(opener)
    visit = visit_document(document, first)
    if visit:
        return visit
    # A Demographics page that runs on over pages of other kinds (an EHR patient-summary
    # print joined by "(continued)") makes the document a Clinical Summary (2026-11-01, user).
    summary = _ARBITRATION.get().get("demographics_summary") or {}
    if (
        first in set(summary.get("opens") or ())
        and any(_family(p) != first for p in document)
        and summary.get("becomes")
    ):
        return str(summary["becomes"]), "demographics_summary"
    # A record document (demographics, problem list, medications, immunizations) that runs
    # long over pages of more than one L1 is a summary print (2026-11-02, user).
    long_run = _ARBITRATION.get().get("long_summary") or {}
    if (
        first in set(long_run.get("opens") or ())
        and len(document) >= int(long_run.get("min_pages", 5))
        and len({_family(p) for p in document if _agreeable(_family(p))}) > 1
        and long_run.get("becomes")
    ):
        return str(long_run["becomes"]), "long_summary"
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


def _visit_settings() -> dict[str, Any]:
    return _ARBITRATION.get().get("visit_document") or {}


def _heading_patterns() -> dict[str, re.Pattern[str]]:
    cfg = _visit_settings()
    key = json.dumps(cfg.get("groups") or {}, sort_keys=True)
    cached = _HEADING_CACHE.get(key)
    if cached is None:
        cached = {
            group: re.compile(
                r"(?im)^[\s*#\-\u2022]*(?:" + "|".join(re.escape(p) for p in sorted(phrases, key=len, reverse=True))
                + r")\s*(?::|$)"
            )
            for group, phrases in (cfg.get("groups") or {}).items() if phrases
        }
        _HEADING_CACHE.clear()
        _HEADING_CACHE[key] = cached
    return cached


_HEADING_CACHE: dict[str, dict[str, re.Pattern[str]]] = {}


def note_sections(text: Optional[str]) -> frozenset[str]:
    """The visit_document groups the page heads: a line that starts with one of the
    group's phrases followed by ':' or nothing else (Chief Complaint:, HPI, Plan:)."""
    if not text:
        return frozenset()
    return frozenset(group for group, pattern in _heading_patterns().items() if pattern.search(text))


def visit_document(document: Sequence[FinalPage], first: Optional[str]) -> Optional[tuple[str, str]]:
    """A document that does not open on a visit note but is one (2026-11-02, user):
    most of its pages are one visit family (more than min_pages - 1 and more than half),
    or its pages head every note-section group and its last page is signed."""
    cfg = _visit_settings()
    families = list(cfg.get("families") or ())
    if not families or first in families or len(document) < 2:
        return None
    counts = {f: sum(1 for p in document if _family(p) == f) for f in families}
    best = max(families, key=lambda f: (counts[f], -families.index(f)))
    if counts[best] >= int(cfg.get("min_pages", 4)) and counts[best] * 2 > len(document):
        return best, "visit_majority"
    groups = set((cfg.get("groups") or {}).keys())
    found = set().union(*(p.sections for p in document))
    if groups and groups <= found and document[-1].signed and cfg.get("becomes"):
        return str(cfg["becomes"]), "note_sections"
    return None


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
        if basis == "last_page":
            opener = document[-1]
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
