"""Keyword model: ``keyword-canon/page_keyword_canon.json`` → a sub-type with a score.

Runs on its own; it never sees BERT's output. The canon's ``how_to_match``
rules, in order:

* Case-insensitive, whole words or whole phrases only.
* Longest match wins: a term inside a longer term that matched at the same
  place counts only as the longer one.
* Title terms weigh ``title_term_in_title_zone`` in the first
  ``title_zone_lines`` lines, ``title_term_elsewhere`` below them. A one-word
  title term is a title only as a heading on its own line; inside a sentence it
  counts as a body term.
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
from typing import Any, Optional

from stages.lib.canon_store import CANON_DIR, CanonFile


@dataclass(frozen=True)
class KeywordResult:
    page_subtype: str
    page_type: str
    model_type: str
    score: float
    margin: float
    title_hit: bool


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


_CANON: CanonFile[_Canon] = CanonFile(CANON_DIR / "page_keyword_canon.json", _build)


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


def classify(text: str) -> Optional[KeywordResult]:
    """The page's best sub-type by keywords, or None when nothing matched."""
    canon = _CANON.get()
    lines = _lines(text)
    if not lines:
        return None
    title_zone_end = lines[min(canon.title_zone_lines, len(lines)) - 1][1]

    # Every match, then longest-match-wins across all terms.
    found = []
    for term in canon.terms:
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
            on_own_line = line is not None and line[2].rstrip(":").strip().lower() == term.text.lower()
            if term.one_word and not on_own_line:
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
        page_type = canon.subtype_page_type[sub]
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
    )
