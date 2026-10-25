"""Keyword matching for the keyword canon (``keyword-canon/page_canon.json``):
a page type with a score.

The axes gate (``axes.py``) builds the canon with ``build`` and ranks its page
types by L1, L2 or L3 (``group_of``). The canon's ``how_to_match`` rules, in
order:

* Case-insensitive, whole words or whole phrases only.
* Longest match wins: a term inside a longer term that matched at the same
  place counts only as the longer one.
* Title terms weigh ``title_term_in_title_zone`` in the first
  ``title_zone_lines`` lines, ``title_term_elsewhere`` below them — but only
  when used as a heading. Inside a sentence ("informed consent was obtained")
  a title term counts as a body term. A heading is a line the OCR's layout
  detected as one (``headings``: the section-header candidates in the Final2 /
  Final1 JSON), a line that is just the term, or a short line that starts with
  it ("OFFICE VISIT REPORT 10/03/2024"). The canon applies this to one-word
  terms; it applies here to every title term.
* Body terms weigh ``body_term``. Ambiguous terms weigh ``ambiguous_term`` and
  only once another term of the same sub-type has matched.
* Each sub-type scores the sum over its distinct matched terms. A page type's
  score is its best sub-type's. The result is the top page type, its best
  sub-type, the score, the margin over the second page type, and whether a
  title term hit in the title zone.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional


@dataclass(frozen=True)
class KeywordResult:
    page_subtype: str
    page_type: str
    model_type: str
    score: float
    margin: float
    title_hit: bool
    # The best-scoring groups, best first.
    top_page_types: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Term:
    text: str
    kind: str  # title | body | ambiguous
    page_subtype: str
    pattern: re.Pattern
    one_word: bool


@dataclass(frozen=True)
class _Canon:
    terms: tuple[_Term, ...]
    subtype_page_type: dict[str, str]
    subtype_model_type: dict[str, str]
    weights: dict[str, float]
    title_zone_lines: int


def _pattern(term: str) -> re.Pattern:
    words = [re.escape(w) for w in term.split()]
    return re.compile(r"(?<![A-Za-z0-9])" + r"\s+".join(words) + r"(?![A-Za-z0-9])", re.IGNORECASE)


def _build(data: dict[str, Any]) -> _Canon:
    terms: list[_Term] = []
    page_types: dict[str, str] = {}
    model_types: dict[str, str] = {}
    for sub in data["sub_types"]:
        name = sub["page_subtype"]
        page_types[name] = sub["page_type"]
        model_types[name] = sub["model_type"]
        for kind in ("title", "body", "ambiguous"):
            for text in sub.get(f"{kind}_terms") or []:
                text = str(text).strip()
                if text:
                    terms.append(_Term(text, kind, name, _pattern(text), len(text.split()) == 1))
    return _Canon(
        terms=tuple(terms),
        subtype_page_type=page_types,
        subtype_model_type=model_types,
        weights={k: float(v) for k, v in data["weights"].items()},
        title_zone_lines=int(data["title_zone_lines"]),
    )


def build(data: dict[str, Any]) -> _Canon:
    """The keyword canon (``page_canon.json``) for ``classify``: each canon page
    type is a sub-type, grouped by its L1, with its path as the model type."""
    sub_types = []
    for entry in data["page_types"]:
        path, lexicon = entry["path"], entry.get("lexicon") or {}
        sub_types.append({
            "page_subtype": entry["page_type"],
            "page_type": path["L1"],
            "model_type": " > ".join(x for x in (path["L1"], path.get("L2"), path.get("L3")) if x),
            **{f"{kind}_terms": lexicon.get(f"{kind}_terms") or [] for kind in ("title", "body", "ambiguous")},
        })
    return _build({"sub_types": sub_types, "weights": data["weights"],
                   "title_zone_lines": data["title_zone_lines"]})


TOP_N = 3


def _lines(text: str) -> list[tuple[int, int, str]]:
    """(start offset, end offset, line) for every non-empty line."""
    out, offset = [], 0
    for raw in (text or "").splitlines(keepends=True):
        stripped = raw.strip()
        if stripped:
            start = offset + raw.index(stripped[0])
            out.append((start, start + len(stripped), stripped))
        offset += len(raw)
    return out


# A line counts as a heading that starts with the title term when it is at
# most this many characters longer than the term.
HEADING_EXTRA_CHARS = 25


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", re.sub(r"\s+", " ", (text or "").lower())).strip()


def is_heading(line: str, term: str, headings: frozenset[str], *, one_word: bool = False) -> bool:
    """Whether ``term`` on ``line`` is used as a heading, not inside a sentence.

    A one-word term must be the whole line or a detected heading (the canon's
    rule); a longer term may also start a short line.
    """
    line_n, term_n = _norm(line), _norm(term)
    if line_n == term_n or line_n in headings:
        return True
    return (
        not one_word
        and line_n.startswith(term_n)
        and len(line_n) <= len(term_n) + HEADING_EXTRA_CHARS
    )


def classify(
    text: str,
    headings: Iterable[str] = (),
    *,
    canon: _Canon,
    subtypes: Optional[frozenset[str]] = None,
    group_of: Optional[dict[str, str]] = None,
) -> Optional[KeywordResult]:
    """The page's best sub-type by keywords, or None when nothing matched.

    ``headings`` are the page's heading lines as the OCR layout detected them
    (section-header candidates); without them only the line's own shape decides.
    ``canon`` is a ``build`` result; ``subtypes`` limits the match to those
    sub-types, and ``group_of`` ranks them by another grouping than their page
    type (the axes gate ranks canon entries by L1, L2 or L3).
    """
    group_of = group_of or canon.subtype_page_type
    lines = _lines(text)
    if not lines:
        return None
    heading_lines = frozenset(_norm(h) for h in headings if h)
    title_zone_end = lines[min(canon.title_zone_lines, len(lines)) - 1][1]

    # Every match, then longest-match-wins across all terms.
    found = []
    for term in canon.terms:
        if subtypes is not None and term.page_subtype not in subtypes:
            continue
        for m in term.pattern.finditer(text):
            found.append((m.start(), m.end(), term))
    found.sort(key=lambda f: (-(f[1] - f[0]), f[0]))
    kept: list[tuple[int, int, _Term]] = []
    for start, end, term in found:
        if any(k_start <= start and end <= k_end and (k_end - k_start) > (end - start)
               for k_start, k_end, _ in kept):
            continue
        kept.append((start, end, term))

    w = canon.weights
    # Per sub-type: the weight of each distinct term, and whether a title hit.
    by_subtype: dict[str, dict[str, float]] = {}
    title_hits: set[str] = set()
    ambiguous: dict[str, dict[str, float]] = {}
    for start, end, term in kept:
        if term.kind == "title":
            line = next((l for l in lines if l[0] <= start < l[1]), None)
            if line is None or not is_heading(line[2], term.text, heading_lines, one_word=term.one_word):
                weight = w["body_term"]
            elif start < title_zone_end:
                weight = w["title_term_in_title_zone"]
                title_hits.add(term.page_subtype)
            else:
                weight = w["title_term_elsewhere"]
        elif term.kind == "body":
            weight = w["body_term"]
        else:
            ambiguous.setdefault(term.page_subtype, {})[term.text.lower()] = w["ambiguous_term"]
            continue
        scores = by_subtype.setdefault(term.page_subtype, {})
        key = term.text.lower()
        scores[key] = max(scores.get(key, 0.0), weight)

    subtype_scores = {
        sub: sum(scores.values()) + sum(ambiguous.get(sub, {}).values())
        for sub, scores in by_subtype.items()
    }
    if not subtype_scores:
        return None

    best_per_type: dict[str, tuple[float, bool, str]] = {}
    for sub, score in subtype_scores.items():
        page_type = group_of[sub]
        key = (score, sub in title_hits)
        if page_type not in best_per_type or key > best_per_type[page_type][:2]:
            best_per_type[page_type] = (score, sub in title_hits, sub)
    ranked = sorted(best_per_type.items(), key=lambda kv: (kv[1][0], kv[1][1]), reverse=True)
    page_type, (score, title_hit, sub) = ranked[0]
    runner_up = ranked[1][1][0] if len(ranked) > 1 else 0.0
    return KeywordResult(
        page_subtype=sub,
        page_type=page_type,
        model_type=canon.subtype_model_type[sub],
        score=round(score, 2),
        margin=round(score - runner_up, 2),
        title_hit=title_hit,
        top_page_types=tuple(name for name, _ in ranked[:TOP_N]),
    )
