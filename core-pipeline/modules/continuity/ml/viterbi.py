"""The whole-chart pass (Brief 3 sections 2.4 and 3A step 7): the most likely sequence of document
starts for a whole chart from every meeting point's calibrated P(break).

States per page: B (begins a document), M (middle), E (ends), S (a one-page document). Allowed:
B->M, B->E, M->M, M->E, E->B, E->S, S->B, S->S; the first page is B or S, the last E or S. The
point between page i and i + 1 is a break exactly when page i is E or S.

Emission per point: log P(break) - logit(threshold) for a break, log(1 - P(break)) for a continue,
so a lone point breaks exactly when P(break) > threshold. Fixed points (hard rules, the visual-page
rule) allow only their decision. ``prior`` is a cost on a one-page document between two pages
(an interior S), against one page squeezed out of a document that runs on; 0 turns it off.

Copied unchanged to training/continuity/clue-model/viterbi.py (a test fails if the copies drift).
"""
from __future__ import annotations

import math
from typing import Any, Optional, Sequence

STATES = ("B", "M", "E", "S")
DECISIVE, VISUAL = "decisive", "visual_continues"
NEXT = {"B": ("M", "E"), "M": ("M", "E"), "E": ("B", "S"), "S": ("B", "S")}
_EPS = 1e-6


def _logit(p: float) -> float:
    p = min(1 - _EPS, max(_EPS, p))
    return math.log(p / (1 - p))


def decode(p_break: Sequence[float], threshold: float, fixed: Optional[Sequence[Optional[bool]]] = None,
           prior: float = 0.0) -> list[bool]:
    """Break (True) / continue (False) for every meeting point of a chart of len(p_break) + 1
    pages. ``fixed[i]``: True / False to force point i, None to leave it to the pass."""
    n_pages = len(p_break) + 1
    if n_pages == 1:
        return []
    fixed = list(fixed) if fixed is not None else [None] * len(p_break)
    bias = -_logit(threshold)

    def emit(i: int, state: str) -> float:
        """Page i's tag decides point i (the point after it); the last page has none."""
        if i == len(p_break):
            return 0.0 if state in ("E", "S") else -math.inf
        brk = state in ("E", "S")
        if fixed[i] is not None and fixed[i] != brk:
            return -math.inf
        p = min(1 - _EPS, max(_EPS, float(p_break[i])))
        return (math.log(p) + bias) if brk else math.log(1 - p)

    def single(i: int, state: str) -> float:
        return -prior if state == "S" and 0 < i < n_pages - 1 else 0.0

    score = {s: (emit(0, s) + single(0, s) if s in ("B", "S") else -math.inf) for s in STATES}
    back: list[dict[str, str]] = []
    for i in range(1, n_pages):
        new, ptr = {}, {}
        for s in STATES:
            best, arg = -math.inf, "B"
            for prev in STATES:
                if s in NEXT[prev] and score[prev] > best:
                    best, arg = score[prev], prev
            new[s] = best + emit(i, s) + single(i, s)
            ptr[s] = arg
        score = new
        back.append(ptr)
    state = max(("E", "S"), key=lambda s: score[s])
    tags = [state]
    for ptr in reversed(back):
        state = ptr[state]
        tags.append(state)
    tags.reverse()
    return [t in ("E", "S") for t in tags[:-1]]


def valid(tags: Sequence[str]) -> bool:
    """A B/M/E/S sequence the pass may output."""
    if not tags:
        return True
    if tags[0] not in ("B", "S") or tags[-1] not in ("E", "S"):
        return False
    return all(b in NEXT[a] for a, b in zip(tags, tags[1:]))


def tags_of(breaks: Sequence[bool]) -> list[str]:
    """The B/M/E/S tag of each page from the breaks between them."""
    n = len(breaks) + 1
    starts = [True] + [bool(b) for b in breaks]
    ends = [bool(b) for b in breaks] + [True]
    return ["S" if starts[i] and ends[i] else "B" if starts[i] else "E" if ends[i] else "M" for i in range(n)]


# --- the fixed points (Brief 3 section 3A steps 2 and 3) ----------------------------------------


def fixed_decision(row: dict[str, Any]) -> Optional[tuple[bool, str, str]]:
    """The hard rules and the visual rule: (break, decided_by, why) or None."""
    if row.get("pageof_a_last") == 1 and row.get("pageof_b_first") == 1:
        return True, DECISIVE, "page N of N, then page 1 of M"
    if row.get("enc_head_b") == 1 and row.get("dos_conflict") == 1:
        return True, DECISIVE, "new visit header with another date of service"
    if row.get("pageof_seq") == 1 and (row.get("header_jacc") or 0) > 0:
        return False, DECISIVE, "page k of N, then page k+1 of N, same running header"
    hard_start = row.get("pageof_b_first") == 1 or row.get("enc_head_b") == 1
    if row.get("visual_b") == 1 and not hard_start:
        return False, VISUAL, "the next page is visual: it continues the document"
    return None
