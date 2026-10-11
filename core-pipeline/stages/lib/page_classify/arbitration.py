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

  Confidence of the page type: from the step that decided it
  (``page_arbitration.json`` → ``confidence``; ``page_type_confidence``).

  Level 2, sub-type inside the winning page type.
  Level 4, codability from the page type (``taxonomy``).

Per document (``decide_final``) — the **Final** answer, on the continuity
stage's documents. The page **type** is agreed across a document's pages; the
**sub-type** stays each page's own.

  The agreed page type (``agree_document_type``), first that applies:
    progress_note  the document opens like a progress note and ends signed
                   (continuity ``in_progress_note``) → Progress Note
    first_page     the first page is confirmed (keyword title, or printed
                   page 1) → the first page's type
    vote           else the page types of the document's pages, weighted by
                   page-type confidence, when one type holds at least
                   ``document_agreement.vote_min_share`` (0.60) of the weight;
                   a tie goes to the first page's type (flagged for review
                   when it changes a page)
    (none)         no clear majority: pages keep their own types, flagged
                   for review (``no_agreement``)

  A page whose own type differs takes the agreed type; its own type becomes
  the sub-type (Laboratory Data → Clinical Summary / Laboratory Data), unless
  the keyword model found a sub-type of the agreed type (score ≥
  keyword_min_score). An Unknown page takes the agreed type's generic
  sub-type. Codability is the agreed type's; inside a Progress Note,
  Laboratory Data / Medication List stay Non-Codable (``inside_progress_note``).

  A page in no document (or a one-page document) keeps its own answer, except
  embedded_same_page: per-page Laboratory Data / Radiology Report with a
  Progress Note title on the same page → Progress Note / that name.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

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
    # Continuity: the document opens like a progress note and ends signed.
    in_progress_note: bool = False
    # The page type's confidence (page_classification.confidence): its vote weight.
    confidence: Optional[float] = None


@dataclass(frozen=True)
class FinalResult:
    page_type: Optional[str]
    page_subtype: Optional[str]
    model_type: Optional[str]
    codability: Optional[str]
    source: str  # page | continuation | embedded | progress_note
    continuation_rule: str
    needs_review: bool


def codability_inside_note(page_subtype: Optional[str]) -> Optional[str]:
    """Codability of a page whose Final type is Progress Note with this
    sub-type: Progress Note's, except the ``inside_progress_note`` sub-types
    (Laboratory Data, Medication List), which stay Non-Codable."""
    taxonomy = tax.load()
    exceptions = (_ARBITRATION.get().get("inside_progress_note") or {}).get("non_codeable_sub_types") or ()
    if page_subtype in exceptions:
        return "Non-Codable"
    return taxonomy.codability(tax.PROGRESS_NOTE)


def _embedded(page: FinalPage, rule: str) -> FinalResult:
    return FinalResult(
        page_type=tax.PROGRESS_NOTE,
        page_subtype=page.page_type,
        model_type=page.page_type,
        codability=codability_inside_note(page.page_type),
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


def _progress_note(page: FinalPage) -> FinalResult:
    """Rule 0: the page is part of a signed progress note. Codability is
    Progress Note's, except Laboratory Data / Medication List."""
    if page.page_type == tax.PROGRESS_NOTE:
        return _keep(page, "progress_note", False)
    unknown = page.page_type in (None, "", UNKNOWN)
    subtype = tax.PROGRESS_NOTE if unknown else page.page_type
    return FinalResult(
        page_type=tax.PROGRESS_NOTE,
        page_subtype=subtype,
        model_type=subtype,
        codability=codability_inside_note(subtype),
        source="progress_note",
        continuation_rule="progress_note",
        needs_review=unknown,
    )


def agree_document_type(document: Sequence[FinalPage]) -> tuple[Optional[str], str]:
    """The page type a document's pages agree on, and why:
    progress_note / first_page / vote (``(None, "")`` for no document)."""
    if not document:
        return None, ""
    if any(p.in_progress_note for p in document):
        return tax.PROGRESS_NOTE, "progress_note"
    first = document[0]
    known = lambda t: t not in (None, "", UNKNOWN)  # noqa: E731
    if known(first.page_type) and (first.start_confirmed or first.keyword_title_hit):
        return first.page_type, "first_page"
    weights: dict[str, float] = {}
    for p in document:
        if known(p.page_type):
            weights[p.page_type] = weights.get(p.page_type, 0.0) + (
                p.confidence if p.confidence is not None else 0.5
            )
    if not weights:
        return None, ""
    best = max(weights.values())
    share = float((_ARBITRATION.get().get("document_agreement") or {}).get("vote_min_share", 0.0))
    if best < share * sum(weights.values()):
        return None, "no_agreement"
    tied = [t for t, w in weights.items() if abs(w - best) < 1e-9]
    if first.page_type in tied:
        return first.page_type, "vote"
    order = [p.page_type for p in document]
    return min(tied, key=order.index), "vote"


def decide_final(
    page: FinalPage, document: Optional[Sequence[FinalPage]] = None
) -> FinalResult:
    """The Final page type of one page. ``document`` is every page of its
    continuity document in order (None / one page: the page stands alone)."""
    if page.in_progress_note:
        return _progress_note(page)
    taxonomy = tax.load()
    if document and len(document) > 1:
        agreed, basis = agree_document_type(document)
        if basis == "progress_note":
            return _progress_note(page)
        if basis == "no_agreement":
            return _keep(page, "no_agreement", True)
        if agreed is None or page.page_type == agreed:
            return _keep(page, "agreed", False)
        min_score = thresholds()["keyword_min_score"]
        if (
            page.keyword_page_subtype
            and taxonomy.page_type_of_subtype(page.keyword_page_subtype) == agreed
            and (page.keyword_score or 0) >= min_score
        ):
            subtype = page.keyword_page_subtype
        elif page.page_type in (None, "", UNKNOWN):
            subtype = agreed
        else:
            subtype = page.page_type
        return FinalResult(
            page_type=agreed,
            page_subtype=subtype,
            model_type=taxonomy.model_type(agreed, subtype),
            codability=(
                codability_inside_note(subtype) if agreed == tax.PROGRESS_NOTE
                else taxonomy.codability(agreed)
            ),
            source="continuation",
            continuation_rule=f"agreed_{basis}",
            needs_review=basis == "vote" or page.link_strength == "weak",
        )
    keyword_type = (
        taxonomy.page_type_of_subtype(page.keyword_page_subtype) if page.keyword_page_subtype else None
    )
    if (
        page.page_type in taxonomy.embedded_page_types
        and keyword_type == tax.PROGRESS_NOTE
        and page.keyword_title_hit
    ):
        return _embedded(page, "embedded_same_page")
    return _keep(page, "no_change", False)
