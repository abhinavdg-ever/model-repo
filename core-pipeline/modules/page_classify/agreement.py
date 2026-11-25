"""Page Type and Codability confidence from how the two approaches agree (2026-11-24, user: "top
matches from both approach types, model and rule"; on the FAMILY since 2026-11-25).

The leaf model and the keyword canon each rank the page's candidate types. The ground truth grades
the page type mostly on its family (the L1: Progress Note, Lab Report ...), and the 2026-11-24 leaf
tiers did not separate accuracy (model 2nd-3rd / same family was the most accurate tier; only "the
families differ" fell). So the tier is read on the chosen type's family F: each approach's families
are ranked by their best path (the first family met is #1).

  A  the model's #1 family and the keywords' #1 family are both F
  B  the model's #1 family is F, the keywords rank F 2nd or 3rd
  C  the model's #1 family is F, the keywords rank F lower or not at all
  D  the model ranks F 2nd or 3rd; with no model, the keywords' #1 family is F
  E  the model ranks F lower or not at all (the approaches disagree on the family), or neither ranks F

Page type confidence starts from the model's probability over every type in F (its family mass;
a leaf's own probability is split between sibling leaves): A that mass; B x ``b_factor``; C x
``c_factor`` and at most ``c_cap`` (Medium); D ``d_base + d_slope x mass`` at most ``d_cap``
(40-69%); E ``e_base + e_slope x mass`` at most ``e_cap`` (20-49%) -- spread, not one value each
(2026-11-25, user). With no model D is ``d_value``, E ``e_value``. An Others leaf keeps
``confidence.backup``.

Codability is right whenever the type's codability is, so its confidence is the model's
probability summed over every candidate with W's codability, then the same check on codability:
both firsts share it -> that sum; one does -> x ``c_factor``, at most ``c_cap``; neither ->
E's spread on that sum. No model: the keywords' first shares it -> ``d_value``, else ``e_value``.

Numbers live in page_keywords.json settings -> confidence -> agreement; review-ui's twin
(services/classification_agreement.py) reads the tier from the stored rankings with this rule.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

SEP = " > "
TIERS = ("A", "B", "C", "D", "E")
# 2026-11-25 (user: "no pile on, the confidences distributed"): D and E follow the model's family
# probability (base + slope x mass, capped under the next band) instead of one fixed value each;
# d_value / e_value stay only for a page with no model (nothing to spread by).
DEFAULTS = {"enabled": True, "b_factor": 0.90, "c_factor": 0.75, "c_cap": 0.79,
            "d_base": 0.40, "d_slope": 0.50, "d_cap": 0.69,
            "e_base": 0.20, "e_slope": 0.50, "e_cap": 0.49,
            "d_value": 0.55, "e_value": 0.30}


def settings(raw: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    return {**DEFAULTS, **(raw or {})}


def _l1(path: Optional[str]) -> str:
    return str(path or "").split(SEP)[0]


def _first(ranks: dict[str, int]) -> Optional[str]:
    return min(ranks, key=lambda p: (ranks[p], p)) if ranks else None


def family_ranks(ranks: dict[str, int]) -> dict[str, int]:
    """{family (L1): rank}: the families in the order of each one's best path, 1 = best."""
    out: dict[str, int] = {}
    for path in sorted(ranks, key=lambda p: (ranks[p], p)):
        out.setdefault(_l1(path), len(out) + 1)
    return out


def family_mass(probs: dict[str, float], family: str) -> float:
    """The model's probability over every type in ``family`` (0-1)."""
    return round(max(0.0, min(1.0, sum(float(p) for path, p in probs.items() if _l1(path) == family))), 4)


def tier(model_family_rank: Optional[int], keyword_family_rank: Optional[int], has_model: bool) -> str:
    """The agreement tier (A-E) of the chosen type's family from its rank in each approach (1 =
    best; None = not ranked)."""
    mr, kr = model_family_rank, keyword_family_rank
    if not has_model:
        return "D" if kr == 1 else "E"
    if mr == 1:
        return "A" if kr == 1 else "B" if kr in (2, 3) else "C"
    return "D" if mr in (2, 3) else "E"


def spread(level: str, p: float, s: dict[str, Any]) -> float:
    """Tier D / E's value: ``<level>_base + <level>_slope x p``, at most ``<level>_cap``."""
    return min(float(s[f"{level}_base"]) + float(s[f"{level}_slope"]) * p, float(s[f"{level}_cap"]))


def page_type_confidence(t: str, p: float, s: dict[str, Any], has_model: bool = True) -> float:
    """``p``: the model's probability over the chosen type's family (``family_mass``)."""
    if t == "A":
        value = p
    elif t == "B":
        value = p * float(s["b_factor"])
    elif t == "C":
        value = min(p * float(s["c_factor"]), float(s["c_cap"]))
    elif t == "D":
        value = spread("d", p, s) if has_model else float(s["d_value"])
    else:
        value = spread("e", p, s) if has_model else float(s["e_value"])
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
    return round(spread("e", mass, s), 4), "neither first shares it"
