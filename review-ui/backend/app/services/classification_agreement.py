"""The page classifier's model / keyword agreement tier, for the Confidence Analysis (2026-11-24,
user), read on the chosen type's FAMILY since 2026-11-25 (core-pipeline page_classify/agreement.py):
each approach's families ranked by their best path.

  A  the model's #1 family and the keywords' #1 family are the chosen type's
  B  the model's #1 family is it, the keywords rank it 2nd or 3rd
  C  the model's #1 family is it, the keywords rank it lower or not at all
  D  the model ranks it 2nd or 3rd; no model: the keywords' #1 family is it
  E  the model ranks it lower or not at all, or neither ranks it

Worked out from the stored rankings, so charts classified before 2026-11-25 (leaf tiers stored)
read the same rule; the stored tier only when the rankings are missing.
"""
from __future__ import annotations

import json
from typing import Any, Optional

TIERS = ("A", "B", "C", "D", "E")
SEP = " > "


def _covers(path: str, leaf: str) -> bool:
    """The model's ``path`` stands for ``leaf``: the leaf itself or a node above it."""
    return leaf == path or leaf.startswith(path + SEP)


def _rank(entries: Any, match) -> Optional[int]:
    ranks = [int(e["rank"]) for e in entries or [] if isinstance(e, dict) and e.get("rank") is not None
             and match(str(e.get("path") or ""))]
    return min(ranks) if ranks else None


def _family_ranks(entries: Any) -> dict[str, int]:
    rows = sorted((int(e["rank"]), str(e.get("path") or "")) for e in entries or []
                  if isinstance(e, dict) and e.get("rank") is not None)
    out: dict[str, int] = {}
    for _rank, path in rows:
        out.setdefault(path.split(SEP)[0], len(out) + 1)
    return out


def tier(value: Any) -> Optional[str]:
    """The tier of one page's classification_agreement JSON; None when it has none."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    if not isinstance(value, dict):
        return None
    leaf = str(value.get("leaf") or "")
    if not leaf or leaf.split(SEP)[0] in ("Others", "Unknown"):
        return None
    model_top, keyword_top = value.get("model_top") or [], value.get("keyword_top") or []
    if not model_top and not keyword_top:
        stored = value.get("tier")
        return stored if stored in TIERS else None
    family = leaf.split(SEP)[0]
    kr = _family_ranks(keyword_top).get(family)
    has_model = (value.get("model_source") or "none") != "none" and bool(model_top)
    if not has_model:
        return "D" if kr == 1 else "E"
    mr = _family_ranks(model_top).get(family)
    if mr == 1:
        return "A" if kr == 1 else "B" if kr in (2, 3) else "C"
    return "D" if mr in (2, 3) else "E"
