"""One confidence scale for every decided value (2026-10-25, user).

Every stage's confidence reads the same way, so a reviewer can trust the number
without knowing which module produced it:

    >= 0.80   good       take it
    0.50-0.80 passable   check when time allows
    <  0.50   review     flagged for manual review

A confidence is never a fixed constant. It is built in three steps:

1. **Base**: the model's probability for the value it decided (a probability over
   all its classes, so already normalised). Where no model exists, the rule's own
   strength times the ``no_model`` penalty.
2. **Backing keywords**: keywords / rules that point to the same value lift it
   (:func:`backed`); keywords that point elsewhere multiply it by
   ``keywords_disagree``.
3. **Penalties**: each reason for doubt multiplies it (:func:`score`) -- a fallback
   decided it, other pages conflict, a later pass overturned it, the page is
   handwritten or nearly empty, a dependency was missing.

Then an optional per-module calibration (fitted from labelled pages) and a clip to
[0, 1]. The numbers live in ``keyword-canon/confidence_canon.json``.
"""
from __future__ import annotations

import bisect
from typing import Any, Iterable, Optional

from stages.lib.canon_store import CANON_DIR, CanonFile

GOOD, PASSABLE, REVIEW = "good", "passable", "review"

DEFAULTS: dict[str, Any] = {
    "bands": {"good": 0.8, "review": 0.5},
    "agree_lift": 0.5,
    "penalties": {
        "keywords_disagree": 0.7, "no_model": 0.85, "fallback": 0.6, "conflict": 0.8,
        "overturned": 0.7, "handwritten": 0.9, "thin_text": 0.85, "degraded": 0.6,
    },
    "thin_text_chars": 150,
    "calibration": {},
}

_CANON: CanonFile[dict[str, Any]] = CanonFile(CANON_DIR / "confidence_canon.json")


def settings() -> dict[str, Any]:
    """The canon's settings over the defaults."""
    try:
        found = _CANON.get()
    except FileNotFoundError:
        found = {}
    return {
        **DEFAULTS,
        **{k: v for k, v in found.items() if k in DEFAULTS},
        "bands": {**DEFAULTS["bands"], **(found.get("bands") or {})},
        "penalties": {**DEFAULTS["penalties"], **(found.get("penalties") or {})},
    }


def settings_for(module: str, defaults: dict[str, Any]) -> dict[str, Any]:
    """A module's own block of the canon (``modules.<module>``) over ``defaults``."""
    try:
        found = (_CANON.get().get("modules") or {}).get(module) or {}
    except FileNotFoundError:
        found = {}
    return {**defaults, **found}


def penalty(name: str) -> float:
    """The multiplier for one reason for doubt (unknown names: 1.0)."""
    return float(settings()["penalties"].get(name, 1.0))


def backed(p: float, support: float) -> float:
    """``p`` lifted by keywords / rules backing the same value: support 1.0 (a strong
    title term, a firing rule) closes ``agree_lift`` of the gap to 1."""
    p, support = _clip(p), _clip(support)
    return p + (1.0 - p) * float(settings()["agree_lift"]) * support


def score(base: Optional[float], *reasons: str, module: str = "") -> Optional[float]:
    """``base`` times the penalty of every reason given (repeats count once), calibrated
    for ``module``, clipped to [0, 1] and rounded. None stays None."""
    if base is None:
        return None
    value = _clip(base)
    for reason in dict.fromkeys(r for r in reasons if r):
        value *= penalty(reason)
    return round(_clip(calibrate(module, value)), 4)


def calibrate(module: str, value: float) -> float:
    """Piecewise-linear map from the canon's ``calibration[module]`` knots; identity
    when the module has none."""
    knots = sorted((float(a), float(b)) for a, b in (settings()["calibration"].get(module) or []))
    if len(knots) < 2:
        return value
    xs = [a for a, _ in knots]
    if value <= xs[0]:
        return knots[0][1]
    if value >= xs[-1]:
        return knots[-1][1]
    i = bisect.bisect_right(xs, value)
    (x0, y0), (x1, y1) = knots[i - 1], knots[i]
    return y0 + (y1 - y0) * (value - x0) / (x1 - x0) if x1 > x0 else y0


def under_bar(p: float, bar: float, *, module: str = "") -> Optional[float]:
    """A value the module decided although the model's probability ``p`` is under the
    module's own bar for being sure (a junk verdict for review below ``junk_at``):
    normalised so the bar itself reads just under the review line -- every such value is
    flagged, and a lower ``p`` reads lower."""
    line = float(settings()["bands"]["review"]) - 0.01
    return score(_clip(p) / max(1e-9, float(bar)) * line, module=module)


def absent(candidate_scores: Iterable[Optional[float]], *reasons: str, module: str = "") -> Optional[float]:
    """How sure "not found" is (2026-10-25, user: a field not found still has a
    confidence). Something was found and turned down: 1 - the best rejected candidate's
    probability (a near miss at 0.7 reads 0.3, flagged). Nothing found at all: 1.0 x
    ``no_model`` (no model scored the absence). ``reasons``: the page's own doubts
    (handwritten, thin text: the OCR may have missed it)."""
    scores = [_clip(s) for s in candidate_scores if s is not None]
    if scores:
        return score(1.0 - max(scores), *reasons, module=module)
    return score(1.0, "no_model", *reasons, module=module)


def page_reasons(text: Optional[str] = None, printed_or_handwritten: Optional[str] = None) -> tuple[str, ...]:
    """Doubt that comes from the page itself: handwritten, or nearly no text."""
    out = []
    if str(printed_or_handwritten or "").lower() in ("handwritten", "mixed"):
        out.append("handwritten")
    if text is not None and len(text.strip()) < int(settings()["thin_text_chars"]):
        out.append("thin_text")
    return tuple(out)


def band(value: Optional[float]) -> Optional[str]:
    """good / passable / review, or None for no value."""
    if value is None:
        return None
    b = settings()["bands"]
    return GOOD if value >= float(b["good"]) else PASSABLE if value >= float(b["review"]) else REVIEW


def needs_review(value: Optional[float]) -> bool:
    """True below the review band. A missing confidence is not a flag by itself."""
    return band(value) == REVIEW


def level(value: Optional[float]) -> Optional[str]:
    """The band as the stored ``confidence_level`` words: high / medium / low."""
    return {GOOD: "high", PASSABLE: "medium", REVIEW: "low"}.get(band(value) or "")


def weakest(values: Iterable[Optional[float]]) -> Optional[float]:
    """The lowest of the values given (None ignored); None when there are none."""
    found = [float(v) for v in values if v is not None]
    return min(found) if found else None


def _clip(value: float) -> float:
    return max(0.0, min(1.0, float(value)))
