"""The local page-order repair (Brief 2 Steps 1-4, 2026-11-22). Pure functions: the pair score
(P(B comes right after A)) is passed in, so the trainer's evaluation and the stage run the same code.

Step 1  **Repair groups.** Continuity's documents (pass 1) are the encounters. A boundary between two
        is *hard* when it is clearly real (``hard_boundary``); every other one is *soft*. An encounter's
        repair group is itself plus the encounter before and after, each joined only across a soft
        boundary with the same DOS; overlapping groups merge into one stretch of pages.
Step 2  **Mismatches** inside a group: a low "comes right after" score, a page number or a fax counter
        going backwards.
Step 3  **Window fix** around each mismatch: the best order of at most ``WINDOW_PAGES`` scan positions
        in which no page moves more than ``WINDOW`` (5) places and pages printing "page n of m" (same m)
        keep their number order. Pages move only when it beats the scan order by ``min_gain``.
Step 4  The order: groups stay in scan order; only pages inside a window may change places.

A wrong move is worse than a missed fix: anything the limits forbid stays in scan order, and a page
number going backwards that no allowed order fixes is ``held`` (flagged for review).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

WINDOW = 5  # the farthest a page moves from its scan position (the user's rule; detect.WINDOW)
WINDOW_PAGES = 10  # scan positions one window search covers (mismatch - 4 .. mismatch + 5)

Score = Callable[[int, int], float]  # log P(page j comes right after page i), page indexes in scan order


@dataclass
class PageInfo:
    number: Optional[tuple[int, int]] = None  # printed "page n of m"
    fax: Optional[tuple[int, int]] = None  # fax counter "P.045/145"
    dos: str = ""


@dataclass
class Result:
    order: list[int]  # scan indexes in the new reading order
    moved: list[int] = field(default_factory=list)  # pages that changed place
    held: list[int] = field(default_factory=list)  # a needed fix the limits did not allow
    windows: list[tuple[int, int, float]] = field(default_factory=list)  # (start, end, gain) applied


def hard_boundary(clue: dict, p_break: Optional[float], hard_break_p: float = 0.95) -> bool:
    """A boundary (continuity's clue row for the meeting point) that sequencing never repairs across:
    different dates of service, a visit header with another date, page numbering restarting with a
    different total, or an end-of-note signature then a title start with P(break) >= hard_break_p.
    (Brief 2 Step 1b; only an end-of-note signature counts, Brief 3 section 2.2a.)"""
    if clue.get("dos_conflict") == 1:
        return True
    if clue.get("pageof_reset") == 1 and clue.get("pageof_total_diff") == 1:
        return True
    if clue.get("sig_tail_a") == 1 and clue.get("title_head_b") == 1 and (p_break or 0.0) >= hard_break_p:
        return True
    return False


def repair_groups(doc_of: Sequence[int], hard: dict[int, bool], dos_of: Sequence[str]) -> list[tuple[int, int]]:
    """Page ranges [start, end] (scan indexes, inclusive) of the merged repair groups: each document
    plus the one before and after across a soft boundary with the same DOS. ``hard[i]`` is True when
    the boundary after page i (between documents) is hard."""
    n = len(doc_of)
    if not n:
        return []
    docs: list[tuple[int, int]] = []
    start = 0
    for i in range(1, n + 1):
        if i == n or doc_of[i] != doc_of[i - 1]:
            docs.append((start, i - 1))
            start = i
    soft = [not hard.get(docs[k][1], False) and (dos_of[docs[k][1]] or "") == (dos_of[docs[k + 1][0]] or "")
            for k in range(len(docs) - 1)]
    spans = []
    for k, (s, e) in enumerate(docs):
        lo = docs[k - 1][0] if k > 0 and soft[k - 1] else s
        hi = docs[k + 1][1] if k + 1 < len(docs) and soft[k] else e
        spans.append((lo, hi))
    merged: list[tuple[int, int]] = []
    for s, e in sorted(spans):
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def _backwards(a: PageInfo, b: PageInfo) -> bool:
    """A printed page number or fax counter going backwards. "m of m" then "1 of m" is the next
    document starting, not backwards."""
    if a.number and b.number and a.number[1] == b.number[1] and b.number[0] < a.number[0]:
        return not (a.number[0] == a.number[1] and b.number[0] == 1)
    if a.fax and b.fax and a.fax[1] == b.fax[1] and b.fax[0] < a.fax[0]:
        return not (a.fax[0] == a.fax[1] and b.fax[0] == 1)
    return False


def _one_numbering(pages: Sequence[int], info: Sequence[PageInfo]) -> set[int]:
    """The page totals that number exactly one document in ``pages``: every page with that total has a
    different number. Two documents printing the same total ("1 of 2", "2 of 2" twice) can't be
    ordered by their numbers."""
    by_total: dict[int, list[int]] = {}
    for p in pages:
        if info[p].number:
            by_total.setdefault(info[p].number[1], []).append(info[p].number[0])
    return {t for t, nums in by_total.items() if len(nums) == len(set(nums))}


def mismatches(order: Sequence[int], score: Score, info: Sequence[PageInfo], cut: float) -> list[int]:
    """Positions k where order[k + 1] does not look like it comes right after order[k]."""
    out = []
    for k in range(len(order) - 1):
        a, b = order[k], order[k + 1]
        if _backwards(info[a], info[b]) or score(a, b) < math.log(max(cut, 1e-9)):
            out.append(k)
    return out


def _order_score(seq: Sequence[int], score: Score, before: Optional[int], after: Optional[int]) -> float:
    total = sum(score(a, b) for a, b in zip(seq, seq[1:]))
    if before is not None and seq:
        total += score(before, seq[0])
    if after is not None and seq:
        total += score(seq[-1], after)
    return total


def best_window(pages: Sequence[int], score: Score, info: Sequence[PageInfo], before: Optional[int],
                after: Optional[int], offset: int = 0) -> list[int]:
    """The best order of ``pages`` (scan indexes) placed at positions offset .. offset + len - 1, with
    every page within WINDOW places of its ORIGINAL scan position (its index) and numbered pages
    (same total) in number order: an exact search over subsets."""
    n = len(pages)
    pos = {p: k for k, p in enumerate(pages)}
    single = _one_numbering(pages, info)

    def allowed(p: int, k: int, placed: int) -> bool:
        if abs(p - (offset + k)) > WINDOW:
            return False
        if not info[p].number or info[p].number[1] not in single:
            return True
        for q in pages:  # a page numbered lower (same total, one document) must already be placed
            if q != p and not placed & (1 << pos[q]) and info[q].number \
                    and info[p].number[1] == info[q].number[1] and info[q].number[0] < info[p].number[0]:
                return False
        return True

    best: dict[tuple[int, int], tuple[float, Optional[tuple[int, int]]]] = {}
    for p in pages:
        if allowed(p, 0, 0):
            best[(1 << pos[p], p)] = (score(before, p) if before is not None else 0.0, None)
    for k in range(1, n):
        layer = {key: v for key, v in best.items() if bin(key[0]).count("1") == k}
        for (mask, last), (val, _prev) in layer.items():
            for p in pages:
                bit = 1 << pos[p]
                if mask & bit or not allowed(p, k, mask):
                    continue
                key = (mask | bit, p)
                cand = val + score(last, p)
                if key not in best or cand > best[key][0]:
                    best[key] = (cand, (mask, last))
    full = (1 << n) - 1
    ends = [(v + (score(last, after) if after is not None else 0.0), (full, last))
            for (mask, last), (v, _p) in best.items() if mask == full]
    if not ends:
        return list(pages)
    _val, key = max(ends)
    seq = []
    while key is not None:
        seq.append(key[1])
        key = best[key][1]
    return seq[::-1]


def repair(n: int, score: Score, info: Sequence[PageInfo], groups: Sequence[tuple[int, int]], *,
           min_gain: float, cut: float) -> Result:
    """Steps 2-4 over the repair groups of one chart (``n`` content pages in scan order)."""
    order = list(range(n))
    res = Result(order)
    for g_start, g_end in groups:
        k = g_start
        while k < g_end:
            seq = order[g_start:g_end + 1]
            local = mismatches(seq, score, info, cut)
            local = [m + g_start for m in local if m + g_start >= k]
            if not local:
                break
            m = local[0]
            lo = max(g_start, m - (WINDOW_PAGES // 2 - 1))
            hi = min(g_end, lo + WINDOW_PAGES - 1)
            window = order[lo:hi + 1]
            before = order[lo - 1] if lo > 0 else None
            after = order[hi + 1] if hi + 1 < n else None
            new = best_window(window, score, info, before, after, offset=lo)
            gain = _order_score(new, score, before, after) - _order_score(window, score, before, after)
            # Printed page numbers are never overruled: a window whose scan order breaks them takes the
            # numbered order whatever the scores say (Brief 2 Step 3).
            single = _one_numbering(window, info)
            numbered_fix = any(_backwards(info[x], info[y]) and info[x].number and info[y].number
                               and info[x].number[1] in single for x, y in zip(window, window[1:]))
            if new != window and (gain > min_gain or numbered_fix):
                order[lo:hi + 1] = new
                res.windows.append((lo, hi, round(gain, 3)))
                k = hi + 1
            else:
                a, b = order[m], order[m + 1]
                if _backwards(info[a], info[b]):
                    res.held += [a, b]
                k = m + 1
    res.order = order
    res.moved = [p for k, p in enumerate(order) if p != k]
    res.held = sorted(set(res.held) - set(res.moved))
    return res
