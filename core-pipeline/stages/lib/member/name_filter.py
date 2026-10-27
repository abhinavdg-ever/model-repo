"""Is a candidate name a person at all? Rules only, no model (2026-10-28, user).

The key/value extraction and NER sometimes hand the member stage a phrase that is not a
name -- "Sars Cov 2", "Elderly Woman", "Dear Ms", "Vitamin D Deficiency", "Amy N
MDOccurred". Those must not become a page's name or count as another member.
keyword-canon/member_name_filter_canon.json holds the word lists; it reloads on change.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from stages.lib.canon_store import CANON_DIR, CanonFile

from .extractors.rule_based.name_common import member_keywords

_TOKEN = re.compile(r"[A-Za-z][A-Za-z'.\-]*|\d+")
# Two capitals then a small letter inside a word: "MDOccurred" (McDonald, DeLeon pass).
_RUN_TOGETHER = re.compile(r"[A-Z]{2,}[a-z]")


@dataclass(frozen=True)
class NameFilter:
    max_words: int
    never_in_name: frozenset[str]
    not_names: frozenset[str]


def _build(data: dict[str, Any]) -> NameFilter:
    return NameFilter(
        max_words=int(data.get("max_words", 4)),
        never_in_name=frozenset(str(w).casefold() for w in data.get("never_in_name") or []),
        not_names=frozenset(str(w).casefold() for w in data.get("not_names") or []),
    )


_FILTER: CanonFile[NameFilter] = CanonFile(CANON_DIR / "member_name_filter_canon.json", _build)


def looks_like_person(name: str) -> bool:
    """False for a phrase that is not a person's name (see the module doc)."""
    text = (name or "").replace(",", " ").strip()
    tokens = _TOKEN.findall(text)
    if not tokens:
        return False
    try:
        rules = _FILTER.get()
    except Exception:  # a missing or broken canon: shape rules only
        rules = NameFilter(4, frozenset(), frozenset())
    if any(token.isdigit() for token in tokens):
        return False
    if len(tokens) > rules.max_words:
        return False
    if any(_RUN_TOGETHER.search(token) and not token.isupper() for token in tokens):
        return False
    words = [token.strip(".'-").casefold() for token in tokens]
    if any(word in rules.never_in_name for word in words):
        return False
    keywords = member_keywords()
    common = rules.not_names | keywords.non_name | keywords.labels
    full = [word for word in words if len(word) > 1]
    if not full:
        return False
    return not all(word in common for word in full)
