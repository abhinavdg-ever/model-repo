"""Page Type and Codability confidence from how the two approaches agree (2026-11-24, user: "top
matches from both approach types, model and rule").

The leaf model and the keyword canon each rank the page's candidate types. When both put the chosen
type first the page is far more often right than when only the model does, yet the old confidence
was the model's probability either way. The tier says how far they agree on the chosen type W:

  A  the model's #1 and the keywords' #1 are both W
  B  the model's #1 is W, the keywords rank W 2nd or 3rd
  C  the model's #1 is W, the keywords rank W lower or not at all
  D  the model ranks W 2nd or 3rd, or its #1 is another type in W's L1 (a Deciding word, a priority
     rule or the keywords chose W); with no model, the keywords' #1 is W
  E  the model's #1 is in another L1 (the approaches disagree on the family), or neither ranks W

Page type confidence: A the model's probability for W; B x ``b_factor``; C x ``c_factor`` and at
most ``c_cap`` (Medium); D ``d_value``; E ``e_value``. An Others leaf keeps ``confidence.backup``.

Codability is right whenever the type's codability is, so its confidence is the model's
probability summed over every candidate with W's codability, then the same check on codability:
both firsts share it -> that sum; one does -> x ``c_factor``, at most ``c_cap``; neither ->
``e_value``. No model: the keywords' first shares it -> ``d_value``, else ``e_value``.

Numbers live in page_leaf_canon.json settings -> confidence -> agreement; review-ui's twin
(services/classification_agreement.py) reads the tier of older rows from the stored ranks.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

SEP = " > "
TIERS = ("A", "B", "C", "D", "E")
DEFAULTS = {"enabled": True, "b_factor": 0.90, "c_factor": 0.75, "c_cap": 0.79, "d_value": 0.55, "e_value": 0.30}


def settings(raw: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    return {**DEFAULTS, **(raw or {})}


def _l1(path: Optional[str]) -> str:
    return str(path or "").split(SEP)[0]


def _first(ranks: dict[str, int]) -> Optional[str]:
    return min(ranks, key=lambda p: (ranks[p], p)) if ranks else None


def tier(model_rank_of_winner: Optional[int], keyword_rank_of_winner: Optional[int],
         model_first_same_l1: bool, has_model: bool) -> str:
    """The agreement tier (A-E) of the chosen type from its rank in each approach (1 = best;
    None = not ranked) and whether the model's #1 is in the chosen type's L1."""
    mr, kr = model_rank_of_winner, keyword_rank_of_winner
    if not has_model:
        return "D" if kr == 1 else "E"
    if mr == 1:
        return "A" if kr == 1 else "B" if kr in (2, 3) else "C"
    if mr in (2, 3) or model_first_same_l1:
        return "D"
    return "E"


def page_type_confidence(t: str, p: float, s: dict[str, Any]) -> float:
    if t == "A":
        value = p
    elif t == "B":
        value = p * float(s["b_factor"])
    elif t == "C":
        value = min(p * float(s["c_factor"]), float(s["c_cap"]))
    elif t == "D":
        value = float(s["d_value"])
    else:
        value = float(s["e_value"])
    return round(max(0.0, min(1.0, value)), 4)


def codability_confidence(codeability: Optional[str], probs: dict[str, float], model_rank: dict[str, int],
                          keyword_rank: dict[str, int], has_model: bool,
                          codeability_of: Callable[[str], Optional[str]], s: dict[str, Any]) -> tuple[Optional[float], str]:
    """(confidence, why) for the page's codability ``codeability``; None with no codability."""
    if not codeability:
        return None, "no codability"
    k_first = _first(keyword_rank)
    k_agrees = bool(k_first) and codeability_of(k_first) == codeability
    if not has_model or not probs:
        return (float(s["d_value"]), "keywords' first shares it, no model") if k_agrees else (
            float(s["e_value"]), "no model, keywords disagree")
    mass = sum(float(p) for path, p in probs.items() if codeability_of(path) == codeability)
    mass = max(0.0, min(1.0, mass))
    m_first = _first(model_rank)
    m_agrees = bool(m_first) and codeability_of(m_first) == codeability
    if m_agrees and (k_agrees or not keyword_rank):
        return round(mass, 4), "model and keywords' firsts share it: model mass"
    if m_agrees or k_agrees:
        return round(min(mass * float(s["c_factor"]), float(s["c_cap"])), 4), "one first shares it"
    return float(s["e_value"]), "neither first shares it"
