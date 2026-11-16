"""Page Type scored with the client-label canon, as the Accuracy View does (groundTruth.ts
pageTypeMark; 2026-11-16, user: Manual Review uses the same rules).

The ground truth's Page Type is a client label ("Echo Report", "Memo", "progressNotes");
the algo's is our path, Page Type + Page Subtype ("Lab Report" + "Diagnostic Study
(Echocardiogram Report)"). ``verdict`` returns "match" / "partial" / "mismatch", or None
when the label is not one the canon knows (the caller then compares the text).

app/data/gtPageTypeCanon.json and pageTaxonomy.json are copies of the frontend's
(review-ui/frontend/src); tests/test_excel_compare_page_type.py keeps them identical.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Optional

DATA = Path(__file__).resolve().parents[2] / "data"


def accept_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


@lru_cache(maxsize=1)
def _canon() -> dict:
    return json.loads((DATA / "gtPageTypeCanon.json").read_text(encoding="utf-8"))["labels"]


@lru_cache(maxsize=1)
def _taxonomy() -> tuple[dict[str, str], dict[str, list[str]]]:
    """Our L1s (key -> name, Others left out) and each L1's L2 keys."""
    data = json.loads((DATA / "pageTaxonomy.json").read_text(encoding="utf-8"))
    l1 = {accept_key(name): name for name in data["L1"] if accept_key(name) != "others"}
    l2 = {accept_key(name): [accept_key(sub) for sub in subs] for name, subs in data["L2"].items()}
    return l1, l2


_NESTED = re.compile(r"^(.+?)\)?\s+\((.+?)\)?$")


def our_path(page_type: str, page_subtype: str) -> list[str]:
    """Our path, L1 first, folded, Others dropped (groundTruth.ts ourPath)."""
    family = (page_type or "").strip()
    sub = (page_subtype or "").strip()
    if accept_key(family) == "invoice":
        # The pipeline's Invoice page is our Administrative > Billing & Invoices.
        return ["administrative", "billinginvoices"]
    _, l2 = _taxonomy()
    l2s = l2.get(accept_key(family), [])
    if not sub:
        below: list[str] = []
    elif sub == family:
        below = [family] if accept_key(family) in l2s else []
    else:
        nested = _NESTED.match(sub)
        below = [sub] if accept_key(sub) in l2s or not nested else [nested.group(1), nested.group(2)]
    return [k for k in (accept_key(level) for level in [family, *below]) if k and k != "others"]


def _starts_with(path: list[str], target: str) -> bool:
    levels = [accept_key(level) for level in target.split(">")]
    if len(levels) > 1 and levels[-1] == "others":
        head = levels[:-1]
        return len(head) == len(path) and head == path
    return len(levels) <= len(path) and levels == path[: len(levels)]


_RANK = {"match": 3, "partial": 2, "mismatch": 1}


def _names(label: str) -> list[str]:
    return [part.strip() for part in re.split(r"\s*[;|]\s*", label or "") if part.strip()]


def verdict(label: str, page_type: str, page_subtype: str) -> Optional[str]:
    """One ground-truth label (or several, "a | b") against one algo path."""
    names = _names(label)
    path = our_path(page_type, page_subtype)
    if not names or not path:
        return None
    l1, _ = _taxonomy()
    keys = [accept_key(name) for name in names]
    # One of our L1s named, and it is ours; or the label names our L2 / L3.
    if any(k in l1 and k == path[0] for k in keys):
        return "match"
    if len(path) > 1 and any(k in path[1:] for k in keys):
        return "match"
    canon = _canon()
    if not all(k in canon for k in keys):
        return None
    marks = []
    for k in keys:
        entry = canon[k]
        if entry["status"] == "exclude":
            continue
        if any(_starts_with(path, t) for t in entry["full"]):
            marks.append("match")
        elif any(accept_key(t.split(">")[0]) == path[0] for t in entry["full"]) or any(
            _starts_with(path, t) for t in entry["partial"]
        ):
            marks.append("partial")
        else:
            marks.append("mismatch")
    return max(marks, key=_RANK.get) if marks else None
