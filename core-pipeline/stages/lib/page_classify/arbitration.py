"""Who wins between BERT and the keyword model, and the Final page type:
``page_arbitration.json``.

The **Extracted** page type is the v24 page axes (``axes.py``); the v1 ladder
below (``classify_page``) still runs for its raw ``bert_*`` / ``keyword_*``
columns only.

Per page (``classify_page``) — the v1 ladder:

  Level 1, page type: the first ladder step that applies wins.
    1 agreement          both give the same page type
    2 bert_high          BERT ≥ bert_high (0.50) — or ≥ bert_low (0.25) with a
                         lead ≥ bert_lead (0.10) over its second choice
    3 keyword_only_class the keyword page type is one BERT was not trained on,
                         keyword score ≥ keyword_min_score with a title hit
    4 keyword_title      keyword title hit and margin ≥ keyword_min_margin
    5 bert_medium        BERT ≥ bert_low                           (review)
    6 unknown            BERT < bert_unknown (0.10)                (review)
    7 top3_agreement     a page type in both models' top 3: the best
                         combined rank (ties: BERT's order)        (review)
    8 keyword_body       keyword score ≥ keyword_min_score          (review)
    9 bert_low           nothing above                             (review)
  Steps 3–4 are "the keyword model, when it is clear". Step 6 applies only with
  a BERT model loaded; without one the keyword model goes on to steps 8–9.
  With neither model answering, the page is Unknown (``no_prediction``).

  Confidence of the page type: from the step that decided it
  (``page_arbitration.json`` → ``confidence``; ``page_type_confidence``).

  Level 2, sub-type inside the winning page type.
  Level 4, codability from the page type (``taxonomy``).

Per document (``decide_final``) — the **Final** answer, on the continuity
stage's documents, in v24 names. The **family** (L1) is agreed across a
document's pages; each page keeps its own names below it.

  The agreed family (``agree_document_type``), first that applies:
    first_page     the first page's L1, whenever it has one
    vote           else (an Unknown first page) the pages' L1s, weighted by
                   page-type confidence, when one holds at least
                   ``document_agreement.vote_min_share`` (0.60) of the weight;
                   a tie goes to the first page's (flagged for review when it
                   changes a page)
    (none)         no clear majority: pages keep their own, flagged for review
                   (``no_agreement``)

  A page of the agreed family keeps its own page type and sub-type. A page of
  another family takes the agreed L1 as its page type, and its own page type
  becomes the sub-type (Progress Note / Lab Report/Result (Radiology)); an
  Unknown page takes the agreed L1 as both.

  Codability comes from the canon (the page's own category). A page that takes
  the agreed family, or whose own canon rule is ``context``, takes the
  document's codability — its first page of that family — except that inside
  a Progress Note a page carrying an ``inside_progress_note.non_codeable_names``
  name (Laboratory, Medication Record…) stays Non-Codable.

  A page in no document (or a one-page document) keeps its own answer, except
  embedded_same_page: a page of an ``embedded_families`` L1 (Lab Report/Result)
  headed by a Progress Note title → Progress Note / its own page type.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from stages.lib.canon_store import CANON_DIR, CanonFile
from stages.lib.page_classify import axes
from stages.lib.page_classify import taxonomy as tax
from stages.lib.page_classify.bert import BertResult
from stages.lib.page_classify.keywords import KeywordResult

_ARBITRATION: CanonFile[dict[str, Any]] = CanonFile(CANON_DIR / "page_arbitration.json")


UNKNOWN = "Unknown"
OTHERS = axes.OTHERS


def thresholds() -> dict[str, float]:
    return {k: float(v) for k, v in _ARBITRATION.get()["thresholds"].items()}


@dataclass(frozen=True)
class PageResult:
    page_type: Optional[str]
    page_subtype: Optional[str]
    model_type: Optional[str]
    codability: Optional[str]
    decided_by: str
    needs_review: bool
    bert_model_type: Optional[str] = None
    bert_confidence: Optional[float] = None
    keyword_page_subtype: Optional[str] = None
    keyword_score: Optional[float] = None
    keyword_margin: Optional[float] = None
    keyword_title_hit: bool = False
    # How sure the decision is (0-1), from the ladder step; None = no prediction.
    confidence: Optional[float] = None
    extra: dict[str, Any] = field(default_factory=dict, compare=False)


def _level_1(
    bert: Optional[BertResult], keyword: Optional[KeywordResult], trained: frozenset[str]
) -> tuple[Optional[str], str, bool]:
    """(page type, step name, needs review)."""
    t = thresholds()
    conf = bert.confidence if bert else 0.0
    if bert and keyword and bert.page_type == keyword.page_type:
        return bert.page_type, "agreement", False
    clear_lead = bert is not None and conf >= t["bert_low"] and bert.lead >= t.get("bert_lead", 1.0)
    if bert and (conf >= t["bert_high"] or clear_lead):
        return bert.page_type, "bert_high", False
    if keyword:
        untrained = keyword.model_type not in trained
        if untrained and keyword.score >= t["keyword_min_score"] and keyword.title_hit:
            return keyword.page_type, "keyword_only_class", False
        if keyword.title_hit and keyword.margin >= t["keyword_min_margin"]:
            return keyword.page_type, "keyword_title", False
    if bert and conf >= t["bert_low"]:
        return bert.page_type, "bert_medium", True
    if bert and conf < t.get("bert_unknown", 0.0):
        return UNKNOWN, "unknown", True
    shared = _top_n_agreement(bert, keyword)
    if shared:
        return shared, "top3_agreement", True
    if keyword and keyword.score >= t["keyword_min_score"]:
        return keyword.page_type, "keyword_body", True
    if bert:
        return bert.page_type, "bert_low", True
    if keyword:
        # No model at all and a weak keyword hit: the best there is, for review.
        return keyword.page_type, "keyword_body", True
    return UNKNOWN, "no_prediction", True


def _top_n_agreement(bert: Optional[BertResult], keyword: Optional[KeywordResult]) -> Optional[str]:
    """A page type in both models' top lists: the best combined rank, ties to BERT's order."""
    if not bert or not keyword:
        return None
    b = list(bert.top_page_types or (bert.page_type,))
    k = list(keyword.top_page_types or (keyword.page_type,))
    shared = [p for p in b if p in k]
    if not shared:
        return None
    return min(shared, key=lambda p: (b.index(p) + k.index(p), b.index(p)))


def keyword_confidence(keyword: Optional[KeywordResult]) -> Optional[float]:
    """The keyword model's own confidence: a base for a title hit or body terms,
    plus a step per point of margin over the next page type, capped."""
    if keyword is None:
        return None
    c = _ARBITRATION.get()["confidence"]
    if keyword.title_hit:
        base, cap = float(c["keyword_title_base"]), float(c["keyword_title_cap"])
    else:
        base, cap = float(c["keyword_body_base"]), float(c["keyword_body_cap"])
    return round(min(cap, base + float(c["keyword_per_margin"]) * max(0.0, keyword.margin)), 4)


def page_type_confidence(
    step: str, review: bool, bert: Optional[BertResult], keyword: Optional[KeywordResult]
) -> Optional[float]:
    """The page type's confidence, from the ladder step that decided it."""
    c = _ARBITRATION.get()["confidence"]
    b = bert.confidence if bert else None
    k = keyword_confidence(keyword)
    if step == "agreement" and b is not None and k is not None:
        value = min(float(c["agreement_cap"]), 1 - (1 - b) * (1 - k))
    elif step.startswith("keyword"):
        value = k
    elif step == "top3_agreement" and b is not None and k is not None:
        value = min(b, k)
    elif step in ("bert_high", "bert_medium", "bert_low", "unknown"):
        value = b
    else:
        value = b if b is not None else k
    if value is None:
        return None
    if review:
        value = min(value, float(c["review_cap"]))
    return round(float(value), 4)


def _level_2(
    page_type: str, bert: Optional[BertResult], keyword: Optional[KeywordResult]
) -> str:
    taxonomy = tax.load()
    keyword_fits = keyword is not None and taxonomy.page_type_of_subtype(keyword.page_subtype) == page_type
    if page_type == tax.PROGRESS_NOTE:
        if bert is not None and bert.page_type == tax.PROGRESS_NOTE:
            return bert.model_type
        if keyword_fits:
            return keyword.page_subtype
        return page_type
    if keyword_fits and keyword.score >= thresholds()["keyword_min_score"]:
        return keyword.page_subtype
    return page_type


def classify_page(
    bert: Optional[BertResult], keyword: Optional[KeywordResult], trained: frozenset[str]
) -> PageResult:
    """Levels 1, 2 and 4 for one page: the Extracted answer."""
    page_type, step, review = _level_1(bert, keyword, trained)
    raw = dict(
        confidence=page_type_confidence(step, review, bert, keyword),
        bert_model_type=bert.model_type if bert else None,
        bert_confidence=bert.confidence if bert else None,
        keyword_page_subtype=keyword.page_subtype if keyword else None,
        keyword_score=keyword.score if keyword else None,
        keyword_margin=keyword.margin if keyword else None,
        keyword_title_hit=bool(keyword and keyword.title_hit),
    )
    if page_type == UNKNOWN:
        return PageResult(UNKNOWN, UNKNOWN, None, None, step, True, **raw)
    subtype = _level_2(page_type, bert, keyword)
    taxonomy = tax.load()
    return PageResult(
        page_type=page_type,
        page_subtype=subtype,
        model_type=taxonomy.model_type(page_type, subtype),
        codability=taxonomy.codability(page_type),
        decided_by=step,
        needs_review=review,
        **raw,
    )


# --- level 3: the Final answer (v24) ---------------------------------------------

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


def _note_rules() -> dict[str, Any]:
    return _ARBITRATION.get().get("inside_progress_note") or {}


def _section_l1() -> str:
    return (_ARBITRATION.get().get("axes") or {}).get("section_L1", tax.PROGRESS_NOTE)


def _inside_note_non_codable(page: FinalPage) -> bool:
    listed = set(_note_rules().get("non_codeable_names") or ())
    return any(name in listed for name in (page.names or (page.page_subtype, page.page_type)))


def _codability_in(agreed: str, page: FinalPage, document_codability: Optional[str]) -> Optional[str]:
    """A page bound into a document of the ``agreed`` family."""
    if agreed == _section_l1() and _inside_note_non_codable(page):
        return CODABILITY_OF["non_codeable"]
    return document_codability


def _names_under(agreed: str, page: FinalPage) -> tuple[str, str]:
    """``page_type`` / ``page_subtype`` for a page bound into a document of the
    ``agreed`` family, so L1, L2 and L3 always agree with the v24 hierarchy:

    * inside a Progress Note, a page of an embedded family is Progress Note /
      its own L2, else its family (Progress Note / Medication List);
    * elsewhere, the L2 (and L3) the page classifier gave the page under that
      L1 (``under_l1``: the model's, else the canon's) -- "L1 (L2)" / L3 or
      L1 / L2;
    * a row classified before ``under_l1`` existed: its own name, when the
      hierarchy places it under that L1;
    * otherwise L1 / Others.
    """
    if agreed == _section_l1():
        if _family(page) in set(_note_rules().get("embedded_families") or ()):
            return agreed, page.l2 or _family(page) or OTHERS
        return agreed, OTHERS
    there = (page.under_l1 or {}).get(agreed) or {}
    if there.get("l2"):
        return (f"{agreed} ({there['l2']})", there["l3"]) if there.get("l3") else (agreed, there["l2"])
    permitted = axes.l2_names(agreed)
    own = (page.l2, *page.names) if page.names else (page.l2, page.page_subtype, page.page_type)
    return agreed, next((name for name in own if name and name in permitted), OTHERS)


def _keep(page: FinalPage, rule: str, review: bool, codability: Optional[str] = None) -> FinalResult:
    # An L1 is never its own sub-type (rows written before page_names said Others).
    repeated = _known(page.page_type) and page.page_subtype == page.page_type
    return FinalResult(
        page_type=page.page_type,
        page_subtype=OTHERS if repeated else page.page_subtype,
        model_type=page.model_type,
        codability=codability or CODABILITY_OF.get(page.category or ""),
        source="page",
        continuation_rule=rule,
        needs_review=review,
    )


def agree_document_type(document: Sequence[FinalPage]) -> tuple[Optional[str], str]:
    """The family (L1) a document's pages agree on, and why: first_page (the
    first page's, whenever it has one) / vote (``(None, "")`` for no
    document)."""
    if not document:
        return None, ""
    first = _family(document[0])
    if _known(first):
        return first, "first_page"
    weights: dict[str, float] = {}
    for p in document:
        family = _family(p)
        if _known(family):
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
        document_codability = CODABILITY_OF.get(opener.category or "")
        if _family(page) == agreed:
            context = page.codeability_rule == "context" and page is not opener
            return _keep(page, "agreed", False,
                         _codability_in(agreed, page, document_codability) if context else None)
        page_type, page_subtype = _names_under(agreed, page)
        return FinalResult(
            page_type=page_type,
            page_subtype=page_subtype,
            model_type=page.model_type,
            codability=_codability_in(agreed, page, document_codability),
            source="continuation",
            continuation_rule=f"agreed_{basis}",
            needs_review=basis == "vote" or page.link_strength == "weak",
        )
    section = _section_l1()
    if _family(page) in set(_note_rules().get("embedded_families") or ()) and page.title_l1 == section:
        return FinalResult(
            page_type=section,
            page_subtype=_names_under(section, page)[1],
            model_type=page.model_type,
            codability=_codability_in(section, page, CODABILITY_OF["codeable"]),
            source="embedded",
            continuation_rule="embedded_same_page",
            needs_review=False,
        )
    return _keep(page, "no_change", False)
