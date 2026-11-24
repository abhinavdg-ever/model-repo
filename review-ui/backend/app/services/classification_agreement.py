"""The page classifier's model / keyword agreement tier (2026-11-24, user), for the Confidence
Analysis' Agreement view: the tier the pipeline stored (core-pipeline page_classify/agreement.py),
else the same rule read from the stored rankings (charts classified before it).

  A  the model's #1 and the keywords' #1 are the chosen type
  B  the model's #1 is it, the keywords rank it 2nd or 3rd
  C  the model's #1 is it, the keywords rank it lower or not at all
  D  the model ranks it 2nd or 3rd, or its #1 is in the same L1; no model: the keywords' #1 is it
  E  the model's #1 is in another L1, or neither ranks it
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


def tier(value: Any) -> Optional[str]:
    """The tier of one page's classification_agreement JSON; None when it has none."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    if not isinstance(value, dict):
        return None
    stored = value.get("tier")
    if stored in TIERS:
        return stored
    leaf = str(value.get("leaf") or "")
    if not leaf or leaf.split(SEP)[0] in ("Others", "Unknown"):
        return None
    model_top, keyword_top = value.get("model_top") or [], value.get("keyword_top") or []
    kr = _rank(keyword_top, lambda p: p == leaf)
    has_model = (value.get("model_source") or "none") != "none" and bool(model_top)
    if not has_model:
        return "D" if kr == 1 else "E"
    mr = _rank(model_top, lambda p: _covers(p, leaf))
    if mr == 1:
        return "A" if kr == 1 else "B" if kr in (2, 3) else "C"
    first = min((e for e in model_top if isinstance(e, dict) and e.get("rank") is not None),
                key=lambda e: int(e["rank"]), default=None)
    same_l1 = bool(first) and str(first.get("path") or "").split(SEP)[0] == leaf.split(SEP)[0]
    return "D" if mr in (2, 3) or same_l1 else "E"
