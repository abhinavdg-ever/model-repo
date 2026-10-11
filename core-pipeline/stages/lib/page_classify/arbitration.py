"""Who wins between BERT and the keyword model: ``page_arbitration.json``.

Per page (``classify_page``) — the **Extracted** answer:

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

  Level 2, sub-type inside the winning page type.
  Level 4, codability from the page type (``taxonomy``).

Per document (``decide_final``) — the **Final** answer, level 3 step C, on the
continuity stage's documents:

  1 continuation          not the start, strong link, start confirmed, per-page
                          Progress Note with no title of its own, document not a
                          Progress Note → the document's page type
  2 embedded_in_document  not the start, strong link, start confirmed, per-page
                          Laboratory Data / Radiology Report, document a Progress
                          Note → Progress Note / that name
  3 embedded_same_page    per-page Laboratory Data / Radiology Report and the
                          keyword model saw a Progress Note title on the page →
                          Progress Note / that name (needs no page order)
  4 possible              1 or 2 would apply but the link is weak or the start
                          unconfirmed → keep, flag for review
  5 no_change             keep
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from stages.lib.canon_store import CANON_DIR, CanonFile
from stages.lib.page_classify import taxonomy as tax
from stages.lib.page_classify.bert import BertResult
from stages.lib.page_classify.keywords import KeywordResult

_ARBITRATION: CanonFile[dict[str, Any]] = CanonFile(CANON_DIR / "page_arbitration.json")


UNKNOWN = "Unknown"


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


# --- level 3, step C ------------------------------------------------------------


@dataclass(frozen=True)
class FinalPage:
    """What level 3 needs about one page (from page_classification + continuity)."""

    page_id: Any
    page_type: Optional[str]
    page_subtype: Optional[str]
    keyword_page_subtype: Optional[str] = None
    keyword_score: Optional[float] = None
    keyword_title_hit: bool = False
    document_seq: Optional[int] = None
    seq: Optional[int] = None  # position within the document; 1 = start
    link_strength: Optional[str] = None  # strong | weak (chain from the start)
    start_confirmed: bool = False


@dataclass(frozen=True)
class FinalResult:
    page_type: Optional[str]
    page_subtype: Optional[str]
    model_type: Optional[str]
    codability: Optional[str]
    source: str  # page | continuation | embedded
    continuation_rule: str
    needs_review: bool


def _embedded(page: FinalPage, rule: str) -> FinalResult:
    taxonomy = tax.load()
    return FinalResult(
        page_type=tax.PROGRESS_NOTE,
        page_subtype=page.page_type,
        model_type=page.page_type,
        codability=taxonomy.codability(tax.PROGRESS_NOTE),
        source="embedded",
        continuation_rule=rule,
        needs_review=False,
    )


def _keep(page: FinalPage, rule: str, review: bool) -> FinalResult:
    taxonomy = tax.load()
    return FinalResult(
        page_type=page.page_type,
        page_subtype=page.page_subtype,
        model_type=(
            taxonomy.model_type(page.page_type, page.page_subtype)
            if page.page_type and page.page_subtype and taxonomy.codability(page.page_type) else None
        ),
        codability=taxonomy.codability(page.page_type),
        source="page",
        continuation_rule=rule,
        needs_review=review,
    )


def decide_final(page: FinalPage, start: Optional[FinalPage]) -> FinalResult:
    """Level 3 step C for one page. ``start`` is its document's first page
    (None for a page in no document, or the start itself)."""
    taxonomy = tax.load()
    embedded = taxonomy.embedded_page_types
    in_document = start is not None and page.seq is not None and page.seq > 1
    strong = page.link_strength == "strong" and page.start_confirmed
    document_type = start.page_type if start else None

    continuation = (
        in_document
        and page.page_type == tax.PROGRESS_NOTE
        and not page.keyword_title_hit
        and document_type not in (None, tax.PROGRESS_NOTE)
    )
    embedded_in_document = (
        in_document and page.page_type in embedded and document_type == tax.PROGRESS_NOTE
    )
    if continuation and strong:
        min_score = thresholds()["keyword_min_score"]
        keyword_fits = (
            page.keyword_page_subtype
            and taxonomy.page_type_of_subtype(page.keyword_page_subtype) == document_type
            and (page.keyword_score or 0) >= min_score
        )
        subtype = page.keyword_page_subtype if keyword_fits else start.page_subtype
        return FinalResult(
            page_type=document_type,
            page_subtype=subtype,
            model_type=taxonomy.model_type(document_type, subtype),
            codability=taxonomy.codability(document_type),
            source="continuation",
            continuation_rule="continuation",
            needs_review=False,
        )
    if embedded_in_document and strong:
        return _embedded(page, "embedded_in_document")
    keyword_type = (
        taxonomy.page_type_of_subtype(page.keyword_page_subtype) if page.keyword_page_subtype else None
    )
    if page.page_type in embedded and keyword_type == tax.PROGRESS_NOTE and page.keyword_title_hit:
        return _embedded(page, "embedded_same_page")
    if continuation or embedded_in_document:
        return _keep(page, "possible", True)
    return _keep(page, "no_change", False)
